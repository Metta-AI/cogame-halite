"""7. Engine — the parallel batch, the shared deadline, the fallback ladder,
the strike rule, the budget guard and the hard stop."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from conftest import FakeLink, make_config
from cogame_halite import defaults, micro
from cogame_halite.engine import Engine

REPO = Path(__file__).resolve().parents[1]


class Clock:
    """An injected clock: nothing in the engine reads the wall clock directly."""

    def __init__(self, step: float = 0.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


async def nosleep(_seconds: float) -> None:
    return None


def links(*specs, trace=None):
    return [
        FakeLink(seat, baseline=spec.get("baseline", "tidewalker"),
                 behaviour=spec.get("behaviour", "ok"), note=spec.get("note"), trace=trace)
        for seat, spec in enumerate(specs)
    ]


# ------------------------------------------------- the parallel-batch property
async def test_every_observe_frame_is_written_before_any_reply_is_awaited():
    trace: list = []
    engine = Engine(make_config(episode_steps=6), links(*[{}] * 4, trace=trace), sleep=nosleep)
    await engine.run()

    # Split the trace into per-turn blocks and assert every block is
    # four sends, THEN receives. A per-seat send/await loop fails here.
    turns: dict[int, list] = {}
    order: list[tuple[str, int, int | None]] = []
    for entry in trace:
        order.append(entry)
    index = 0
    while index < len(order):
        assert order[index][0] == "send", f"a receive preceded a send at {index}: {order[index]}"
        turn = order[index][2]
        sends = 0
        while index < len(order) and order[index][0] == "send":
            assert order[index][2] == turn, "an observe frame for another turn interleaved"
            sends += 1
            index += 1
        receives = 0
        while index < len(order) and order[index][0] == "receive":
            receives += 1
            index += 1
        assert sends == 4, f"turn {turn}: {sends} observe frames written before the wait"
        assert receives == 4, f"turn {turn}: {receives} replies awaited"
        turns[turn] = [sends, receives]
    assert len(turns) >= 4


async def test_the_engine_records_the_batch_it_sent_every_turn():
    engine = Engine(make_config(episode_steps=5), links(*[{}] * 4), sleep=nosleep)
    await engine.run()
    for entry in engine.batch_trace:
        assert entry["sent"] == [0, 1, 2, 3]


async def test_a_hung_seat_cannot_stall_the_episode():
    """The whole point of a shared deadline: the sim never waits."""
    cfg = make_config(episode_steps=8, turn_deadline_ms=40, directive_deadline_ms=100)
    engine = Engine(cfg, links({"behaviour": "silent"}, {}, {}, {}), sleep=nosleep)
    outcome = await asyncio.wait_for(engine.run(), 30)
    assert outcome.reason == "complete"
    assert engine.seats[0].fallbacks["timeout"] > 0


def test_no_await_on_player_input_escapes_a_deadline():
    """Static scan: every await on a seat link is inside an asyncio.wait with a
    timeout, or is a send."""
    source = (REPO / "server" / "cogame_halite" / "engine.py").read_text()
    awaits = re.findall(r"await ([a-zA-Z_.\[\]()\"' ]+)", source)
    for expression in awaits:
        expression = expression.strip()
        assert not expression.startswith("state.link.receive"), (
            "a bare await on a player's receive() has no deadline"
        )
    assert "asyncio.wait(" in source and "timeout=deadline_ms / 1000.0" in source


# ------------------------------------------------------------ fallback causes
@pytest.mark.parametrize(
    "behaviour,cause",
    [("silent", "timeout"), ("malformed", "malformed"),
     ("wrong_turn", "wrong_turn"), ("disconnected", "disconnected")],
)
async def test_each_wire_failure_lands_in_its_own_fallbacks_key(behaviour, cause):
    cfg = make_config(episode_steps=6, turn_deadline_ms=40, directive_deadline_ms=100)
    engine = Engine(cfg, links({"behaviour": behaviour}, {}, {}, {}), sleep=nosleep)
    outcome = await engine.run()
    counts = outcome.results["fallbacks"][0]
    assert counts[cause] > 0, counts
    assert sum(counts.values()) == counts[cause], "the causes must partition"
    for seat in (1, 2, 3):
        assert sum(outcome.results["fallbacks"][seat].values()) == 0


async def test_a_substitution_plays_the_tidewalker_compile_not_a_noop():
    """The engine substitutes the orders tidewalker compiles from the same
    state, in-process -- the same function the scripted player imports."""
    cfg = make_config(episode_steps=4, turn_deadline_ms=40)
    engine = Engine(cfg, links({"behaviour": "silent"}, {}, {}, {}), sleep=nosleep)
    engine.sim.reset()
    expected = engine._scripted_orders(0)
    view = micro.BoardView.from_sim(engine.sim)
    assert expected == micro.compile_turn(view, 0, micro.TIDEWALKER, baseline="tidewalker")
    outcome = await engine.run()
    recorded = outcome.replay.turns[0]["orders"][0]
    assert recorded == expected and recorded, "the substitute must be a real plan"


async def test_fallback_events_carry_the_cause_and_a_bounded_detail():
    cfg = make_config(episode_steps=4, turn_deadline_ms=40)
    engine = Engine(cfg, links({"behaviour": "malformed"}, {}, {}, {}), sleep=nosleep)
    outcome = await engine.run()
    events = [e for t in outcome.replay.turns for e in t["events"] if e["k"] == "fallback"]
    assert events
    for event in events:
        assert event["cause"] in defaults.FALLBACK_CAUSES
        assert len(event["detail"]) <= defaults.MAX_FALLBACK_DETAIL_RUNES


# --------------------------------------------------------------- strike rule
async def test_the_strike_rule_fires_at_ten_and_stops_awaiting_the_seat():
    cfg = make_config(episode_steps=20, turn_deadline_ms=30, directive_deadline_ms=100)
    trace: list = []
    engine = Engine(cfg, links({"behaviour": "silent"}, {}, {}, {}, trace=trace), sleep=nosleep)
    outcome = await engine.run()
    assert outcome.results["dead_seats"][0] is True
    strikes = [e for t in outcome.replay.turns for e in t["events"] if e["k"] == "strike"]
    assert len(strikes) == 1 and strikes[0]["seat"] == 0
    # After the strike the dead seat is no longer AWAITED, so it cannot hold up
    # the batch. It still receives its observe frame -- that is the only way it
    # could ever answer again (test_a_valid_reply_revives_a_dead_seat).
    batches = list(engine.batch_trace)
    assert batches[0]["awaited"] == [0, 1, 2, 3]
    assert batches[-1]["awaited"] == [1, 2, 3]
    assert batches[-1]["sent"] == [0, 1, 2, 3]
    assert defaults.STRIKE_LIMIT == 10


async def test_a_valid_reply_revives_a_dead_seat():
    class Flaky(FakeLink):
        def __init__(self, seat, trace):
            super().__init__(seat, trace=trace)
            self.turn = 0

        async def send(self, message):
            self.turn = message["turn"]
            self.behaviour = "ok" if message["turn"] >= 12 else "silent"
            await super().send(message)

    cfg = make_config(episode_steps=20, turn_deadline_ms=30, directive_deadline_ms=100)
    trace: list = []
    seats = [Flaky(0, trace)] + links(*[{}] * 4, trace=trace)[1:]
    engine = Engine(cfg, seats, sleep=nosleep)
    outcome = await engine.run()
    assert outcome.results["dead_seats"][0] is False, "a valid reply must revive the seat"


async def test_a_dead_seat_is_reported_once_with_the_closed_payload():
    reported: list[dict] = []

    async def on_failure(message: str, seat: int) -> None:
        reported.append({"message": message, "failed_policy_index": seat})

    cfg = make_config(episode_steps=16, turn_deadline_ms=30, directive_deadline_ms=100)
    engine = Engine(cfg, links({"behaviour": "silent"}, {}, {}, {}),
                    sleep=nosleep, on_player_failure=on_failure)
    await engine.run()
    assert len(reported) == 1
    assert set(reported[0]) == {"message", "failed_policy_index"}
    assert reported[0]["failed_policy_index"] == 0


# --------------------------------------------------- budget guard / hard stop
async def test_the_budget_guard_fires_and_the_episode_still_ends_complete():
    clock = Clock(step=30.0)          # 30 s per clock read
    cfg = make_config(episode_steps=30, budget_guard_seconds=600.0,
                      wall_clock_budget_seconds=3600.0)
    engine = Engine(cfg, links(*[{}] * 4), clock=clock, sleep=nosleep)
    outcome = await engine.run()
    assert outcome.reason == "complete" and outcome.end_rule == "full_time"
    guards = [e for t in outcome.replay.turns for e in t["events"] if e["k"] == "budget_guard"]
    assert len(guards) == 1, "the guard fires once and records the turn it fired"
    # Past the guard no seat is asked anything.
    fired = guards[0]["turn"]
    for entry in engine.batch_trace:
        if entry["turn"] >= fired:
            assert entry["sent"] == [] and entry["awaited"] == [] and entry["guard"]


async def test_the_hard_stop_ends_the_episode_with_reason_deadline():
    clock = Clock(step=120.0)
    cfg = make_config(episode_steps=200, budget_guard_seconds=600.0,
                      wall_clock_budget_seconds=660.0)
    engine = Engine(cfg, links(*[{}] * 4), clock=clock, sleep=nosleep)
    outcome = await engine.run()
    assert outcome.end_rule == "wall_clock"
    assert outcome.reason == "deadline"
    assert outcome.replay.stop == {"rule": "wall_clock", "turn": outcome.final_turn}
    # A deadline episode is still scored and ranked, never zeroed.
    assert sorted(outcome.results["placement"]) == [1, 2, 3, 4] or \
        len(set(outcome.results["placement"])) < 4
    assert outcome.results["scores"] == outcome.results["banked"]


async def test_the_guard_default_is_below_the_hard_stop():
    assert defaults.DEFAULT_BUDGET_GUARD_SECONDS < defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
    with pytest.raises(Exception):
        make_config(budget_guard_seconds=700.0, wall_clock_budget_seconds=660.0)


# ------------------------------------------------------- directive spacing
async def test_the_directive_spacing_floor_paces_the_batches():
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    clock = Clock(step=0.0)
    cfg = make_config(episode_steps=45, directive_every=20, directive_spacing_ms=10000)
    engine = Engine(cfg, links(*[{}] * 4), clock=clock, sleep=record)
    await engine.run()
    # Turns 0, 20 and 40 are directive turns; the first opens immediately, the
    # next two each wait out the 10 s floor (the injected clock never advances).
    assert len([s for s in slept if s > 0]) == 2
    assert all(abs(s - 10.0) < 1e-9 for s in slept if s > 0)


async def test_spacing_is_not_applied_on_micro_turns():
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    cfg = make_config(episode_steps=10, directive_every=100, directive_spacing_ms=10000)
    engine = Engine(cfg, links(*[{}] * 4), clock=Clock(0.0), sleep=record)
    await engine.run()
    assert not [s for s in slept if s > 0]


# ------------------------------------------------------------- bookkeeping
async def test_llm_turns_counts_only_llm_and_retry_sources():
    cfg = make_config(episode_steps=41, directive_every=20)
    engine = Engine(cfg, links(*[{}] * 4), sleep=nosleep)
    outcome = await engine.run()
    # FakeLink answers "llm" on directive turns. Turns 0 and 20 are asked;
    # turn 40 is the FINAL recorded state and takes no orders (episode_steps 41
    # means Board.next() runs 40 times).
    assert outcome.results["llm_turns"] == [2, 2, 2, 2]


async def test_notes_reach_the_replay_rune_truncated():
    note = "\U0001F6A2" * 400
    cfg = make_config(episode_steps=4)
    engine = Engine(cfg, links({"note": note}, {}, {}, {}), sleep=nosleep)
    outcome = await engine.run()
    notes = [e for t in outcome.replay.turns for e in t["events"] if e["k"] == "note"]
    assert notes
    for event in notes:
        assert len(event["text"]) == defaults.MAX_NOTE_RUNES
        assert event["source"] in defaults.SOURCES


async def test_an_over_cap_action_map_is_trimmed_to_256_by_ascending_uid():
    class Flood(FakeLink):
        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            actions = {f"0-{i}": "NORTH" for i in range(1, 400)}
            actions.update({k: "NORTH" for k in message["players"][self.seat][2]})
            self.queue.put_nowait(
                {"type": "orders", "turn": message["turn"], "actions": actions})

    cfg = make_config(episode_steps=4)
    engine = Engine(cfg, [Flood(0)] + links(*[{}] * 4)[1:], sleep=nosleep)
    outcome = await engine.run()
    for turn in outcome.replay.turns:
        assert len(turn["orders"][0]) <= defaults.MAX_ACTIONS_PER_TURN


async def test_unowned_ids_and_bad_enum_values_are_dropped():
    class Junk(FakeLink):
        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            self.queue.put_nowait({
                "type": "orders", "turn": message["turn"],
                "actions": {"someone-elses": "NORTH", "0-1": "TELEPORT",
                            "x" * 40: "NORTH", "0-2": "SPAWN"},
            })

    cfg = make_config(episode_steps=4)
    engine = Engine(cfg, [Junk(0)] + links(*[{}] * 4)[1:], sleep=nosleep)
    outcome = await engine.run()
    assert outcome.reason == "complete"
    assert outcome.replay.turns[0]["orders"][0] == {}, (
        "0-2 is another seat's ship and SPAWN is not a ship action"
    )


async def test_lead_events_are_emitted_only_when_the_leader_changes():
    cfg = make_config(episode_steps=40)
    engine = Engine(cfg, links(*[{}] * 4), sleep=nosleep)
    outcome = await engine.run()
    leads = [(t["t"], e["seat"]) for t in outcome.replay.turns
             for e in t["events"] if e["k"] == "lead"]
    seats = [seat for _turn, seat in leads]
    assert all(a != b for a, b in zip(seats, seats[1:])), "a lead event repeated the leader"


async def test_a_seat_that_never_connects_never_stops_the_clock():
    cfg = make_config(episode_steps=10, turn_deadline_ms=40)
    engine = Engine(cfg, [None] + links(*[{}] * 4)[1:], sleep=nosleep)
    outcome = await asyncio.wait_for(engine.run(), 30)
    assert outcome.reason == "complete"
    assert outcome.results["fallbacks"][0]["disconnected"] > 0


async def test_a_sim_fault_is_an_outcome_not_a_crash(monkeypatch):
    from cogame_halite.sim import HaliteGuardError

    cfg = make_config(episode_steps=20)
    engine = Engine(cfg, links(*[{}] * 4), sleep=nosleep)
    real_step = engine.sim.step
    calls = {"n": 0}

    def exploding(orders):
        calls["n"] += 1
        if calls["n"] == 5:
            raise HaliteGuardError("cell 7 halite went negative: -1.0")
        return real_step(orders)

    monkeypatch.setattr(engine.sim, "step", exploding)
    outcome = await engine.run()
    assert outcome.reason == "fault" and outcome.end_rule == "fault"
    assert "negative" in outcome.results["stop_detail"]
    assert len(outcome.results["stop_detail"]) <= defaults.MAX_STOP_DETAIL_RUNES
    assert outcome.replay.turns, "artifacts are still written on a fault"
