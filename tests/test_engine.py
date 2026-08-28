"""7. Engine — the parallel batch, the shared deadline, the fallback ladder,
the strike rule, the budget guard and the hard stop."""

from __future__ import annotations

import asyncio
import json
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
    """Static scan: every await on a seat link — the reply AND the write — is
    under a timeout.

    The write half is the r2-F2 scar: `await state.link.send(frame)` with no
    bound parks on the transport's drain waiter the moment a peer stops
    reading its socket, and nothing downstream (strike rule, budget guard,
    hard stop) is evaluated again until the batch returns.
    """
    source = (REPO / "server" / "cogame_halite" / "engine.py").read_text()
    awaits = re.findall(r"await ([a-zA-Z_.\[\]()\"' ]+)", source)
    for expression in awaits:
        expression = expression.strip()
        assert not expression.startswith("state.link.receive"), (
            "a bare await on a player's receive() has no deadline"
        )
        assert not expression.startswith("state.link.send"), (
            "a bare await on a player's send() has no deadline (r2-F2)"
        )
    assert "asyncio.wait_for(state.link.send(frame), budget)" in source
    assert "asyncio.wait(" in source and "timeout=budget" in source
    assert "budget = deadline_ms / 1000.0" in source, (
        "the writes and the replies share ONE turn deadline"
    )


async def test_a_seat_that_stops_reading_its_socket_cannot_stall_the_batch():
    """r2-F2, reproduced in process: a peer that holds its link open but never
    drains it makes `send` block forever. Bounded, the batch still finishes,
    the seat is substituted as `disconnected`, and the link is dropped rather
    than costing the deadline again every turn."""

    class Blocked(FakeLink):
        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            await asyncio.Event().wait()  # never drains, never raises

    cfg = make_config(episode_steps=8, turn_deadline_ms=40, directive_deadline_ms=100)
    logged: list[str] = []
    engine = Engine(cfg, [Blocked(0)] + links(*[{}] * 4)[1:], sleep=nosleep,
                    log=logged.append)
    outcome = await asyncio.wait_for(engine.run(), 30)

    assert outcome.reason == "complete"
    assert engine.seats[0].fallbacks["disconnected"] > 0
    assert engine.seats[0].link is None, "a link that will not drain is dropped"
    assert any("STOPPED READING" in line for line in logged), logged
    # The other three seats keep playing. They lose exactly the one turn whose
    # deadline the blocked write spent -- counted as `host_error`, because
    # nothing is wrong with THEIR peers -- and their links survive.
    assert engine.seats[2].fallbacks["host_error"] == 1
    assert engine.seats[2].fallbacks["disconnected"] == 0
    assert engine.seats[2].link is not None
    assert outcome.replay.turns[-1]["t"] == 7


async def test_a_blocked_write_costs_the_batch_one_deadline_not_two():
    """The writes share the turn's deadline with the replies, so the worst a
    blocked socket can add to a turn is ONE `deadlineMs` — which is what the
    718 s <= 720 s container arithmetic assumes (see
    `tests/test_server.py::test_the_worst_case_container_time_fits_inside_the_platform_pin`).
    A seat the block deprived of its window is a `host_error`, never a
    `timeout`: nothing is wrong with its peer."""

    class Blocked(FakeLink):
        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            await asyncio.Event().wait()

    class Slow(FakeLink):
        """A healthy peer that does not answer instantly."""

        async def receive(self):
            await asyncio.sleep(0.2)
            return await super().receive()

    cfg = make_config(episode_steps=2, turn_deadline_ms=300, directive_deadline_ms=300,
                      directive_every=1)
    # Seat 3 blocks, so seats 0-2 are written to first and awaited after it.
    seats = [Slow(seat) for seat in range(3)] + [Blocked(3)]
    engine = Engine(cfg, seats, sleep=nosleep)
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await asyncio.wait_for(engine.run(), 30)
    spent = loop.time() - started

    assert outcome.reason == "complete"
    assert engine.seats[3].fallbacks["disconnected"] == 1
    for seat in (0, 1, 2):
        assert engine.seats[seat].fallbacks["host_error"] == 1
        assert engine.seats[seat].fallbacks["timeout"] == 0
    assert spent < 0.45, (
        f"the blocked-write turn took {spent:.2f}s: the writes and the replies "
        "must share one deadline (0.3 s), not take one each"
    )


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


async def test_several_dead_seats_are_one_payload_that_names_them_all():
    """The payload is closed and carries exactly ONE `failed_policy_index`,
    and the failure channel is a URI write (a second write replaces the
    first), so a report per seat would lose the earlier one. One payload: the
    lowest dead seat as the index, every dead seat in the message."""
    reported: list[dict] = []

    async def on_failure(message: str, seat: int) -> None:
        reported.append({"message": message, "failed_policy_index": seat})

    cfg = make_config(episode_steps=16, turn_deadline_ms=30, directive_deadline_ms=100)
    engine = Engine(
        cfg,
        links({}, {"behaviour": "silent"}, {}, {"behaviour": "silent"}),
        sleep=nosleep,
        on_player_failure=on_failure,
    )
    outcome = await engine.run()

    assert outcome.results["dead_seats"] == [False, True, False, True]
    assert len(reported) == 1
    assert set(reported[0]) == {"message", "failed_policy_index"}
    assert reported[0]["failed_policy_index"] == 1, "the lowest dead seat"
    assert "1 (FLEET-BRAVO)" in reported[0]["message"]
    assert "3 (FLEET-DELTA)" in reported[0]["message"], (
        "a second failing seat must not be silent on this channel"
    )


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
        sizes: list[int] = []

        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            actions = {f"0-{i}": "NORTH" for i in range(1, 400)}
            actions.update({k: "NORTH" for k in message["players"][self.seat][2]})
            Flood.sizes.append(len(actions))
            self.queue.put_nowait(
                {"type": "orders", "turn": message["turn"], "actions": actions})

    Flood.sizes = []
    cfg = make_config(episode_steps=4)
    logged: list[str] = []
    engine = Engine(cfg, [Flood(0)] + links(*[{}] * 4)[1:], sleep=nosleep,
                    log=logged.append)
    outcome = await engine.run()
    for turn in outcome.replay.turns:
        assert len(turn["orders"][0]) <= defaults.MAX_ACTIONS_PER_TURN

    # ... and every entry past the cap is DROPPED AND COUNTED, not silently
    # trimmed (the design note's reply-cap table).
    expected = sum(size - defaults.MAX_ACTIONS_PER_TURN for size in Flood.sizes)
    assert expected > 0 and engine.seats[0].dropped_over_cap == expected
    assert engine.seats[1].dropped_over_cap == 0
    assert any("over the 256 cap were dropped" in line for line in logged), logged
    # The reply itself was USED, so this is an audit counter and not a
    # fallback cause: the closed `fallbacks` key set is untouched.
    assert not any(engine.seats[0].fallbacks.values())


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


# ------------------------------------------- the frame is Kaggle's observation
class RecordingLink(FakeLink):
    """A seat link that keeps every frame the engine wrote to it."""

    def __init__(self, seat: int, **kwargs):
        super().__init__(seat, **kwargs)
        self.frames: list[dict] = []

    async def send(self, message: dict) -> None:
        self.frames.append(json.loads(json.dumps(message)))
        await super().send(message)


async def test_a_kaggle_bots_board_builds_from_the_wire_frame_unchanged():
    """`docs/PROTOCOL.md`: "a Kaggle bot's `Board(obs, config)` works
    unchanged". `Board.__init__` reads `observation.step` and
    `observation.remaining_overage_time`, so a frame without `step` and
    `remainingOverageTime` raises `KeyError('step')` — and the hundreds of open
    leaderboard bots the design note points at are not portable after all.

    This drives a real episode, takes the bytes the engine actually wrote to a
    seat's socket, and builds a board out of them with the VENDORED helpers."""
    from cogame_halite.sim import Board  # the vendored class, not a re-write

    cfg = make_config(episode_steps=6)
    seats = [RecordingLink(seat) for seat in range(4)]
    engine = Engine(cfg, seats, sleep=nosleep)
    await engine.run()

    frames = seats[2].frames
    assert frames, "the seat received no observe frame"
    for frame in frames:
        assert frame["step"] == frame["turn"], "`turn` is our spelling of `step`"
        assert frame["remainingOverageTime"] == (
            defaults.upstream_spec()["observation"]["remainingOverageTime"]
        )
        board = Board(frame, cfg.upstream_configuration())
        assert board.step == frame["turn"]
        assert board.current_player_id == 2
        assert len(board.players) == 4
        assert len(board.cells) == cfg.size * cfg.size
        for seat, (bank, yards, ships) in enumerate(frame["players"]):
            player = board.players[seat]
            assert player.halite == bank
            assert sorted(s.id for s in player.ships) == sorted(ships)
            assert sorted(y.id for y in player.shipyards) == sorted(yards)


async def test_the_budget_can_be_measured_from_an_instant_the_caller_supplies():
    """The server hands the engine PROCESS start, so the lobby is spent inside
    the 600 s guard and the 660 s hard stop rather than before them."""
    clock = Clock(step=0.0)
    clock.now = 500.0
    engine = Engine(
        make_config(episode_steps=4),
        links(*[{}] * 4),
        clock=clock,
        sleep=nosleep,
        started_at=200.0,
    )
    assert engine.elapsed == 300.0

    # A 130 s lobby plus a 540 s episode trips the guard; measured from the
    # engine's own construction it would not have.
    guarded = Engine(
        make_config(episode_steps=4),
        links(*[{}] * 4),
        clock=Clock(step=610.0),
        sleep=nosleep,
        started_at=0.0,
    )
    outcome = await guarded.run()
    assert guarded.budget_guard_fired
    assert outcome.reason == "deadline"
