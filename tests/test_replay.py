"""8. End to end — a real episode, strict-UTF-8 replay bytes, and the
record -> re-derive check for **every** end reason."""

from __future__ import annotations

import json

import pytest

from conftest import FakeLink, make_config
from cogame_halite import defaults, events
from cogame_halite.engine import Engine
from cogame_halite.replay import ReplayError, parse
from cogame_halite.sim import HaliteSim
from cogame_halite.version import GAME_VERSION, PROTOCOL, REPLAY_FORMAT, REPLAY_VERSION

EMOJI_NOTE = "\U0001F6A2 squeezing BRAVO off the north cluster \U0001F9C2 " * 20


async def nosleep(_seconds: float) -> None:
    return None


class Clock:
    def __init__(self, step: float = 0.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


async def play(**overrides) -> tuple[Engine, object]:
    note = overrides.pop("note", EMOJI_NOTE)
    clock = overrides.pop("clock", None)
    links = overrides.pop("links", None)
    cfg = make_config(**overrides)
    seats = links or [
        FakeLink(i, baseline=("tidewalker", "corsair")[i % 2], note=note) for i in range(4)
    ]
    engine = Engine(cfg, seats, sleep=nosleep, **({"clock": clock} if clock else {}))
    outcome = await engine.run()
    return engine, outcome


# ------------------------------------------------------------ the document
async def test_a_real_120_turn_episode_writes_a_parseable_replay():
    _engine, outcome = await play(episode_steps=120)
    raw = outcome.replay.to_bytes()
    # STRICT utf-8, then json. A byte-boundary truncation renders in a browser
    # and fails right here.
    text = raw.decode("utf-8")
    doc = json.loads(text)
    assert doc["format"] == REPLAY_FORMAT
    assert doc["version"] == REPLAY_VERSION
    assert doc["gameVersion"] == GAME_VERSION
    assert doc["protocol"] == PROTOCOL
    assert doc["coworld"] == "halite"
    assert len(doc["turns"]) == 120
    parse(raw)


async def test_the_header_is_self_sufficient():
    _engine, outcome = await play(episode_steps=30)
    doc = outcome.replay.document()
    assert doc["names"] == ["daveey", "daveey-1", "halite-tidewalker", "halite-corsair"]
    assert doc["aliases"] == list(defaults.ALIASES)
    assert doc["colors"] == list(defaults.COLORS)
    assert doc["seed"] == 42
    assert doc["config"]["size"] == 21
    assert doc["config"]["episodeSteps"] == 30
    assert len(doc["policySources"]) == 4
    assert doc["results"]["reason"] == "complete"
    # Everything the viewer needs is in the bytes: it contacts S3 and nothing
    # else, so any missing key is a blank viewer.
    for key in ("names", "aliases", "colors", "config", "seed", "turns", "results", "stop"):
        assert doc[key] is not None


async def test_notes_survive_a_strict_parse_at_the_cap():
    _engine, outcome = await play(episode_steps=30, note=EMOJI_NOTE)
    raw = outcome.replay.to_bytes()
    doc = json.loads(raw.decode("utf-8"))
    notes = [e for t in doc["turns"] for e in t["events"] if e["k"] == "note"]
    assert notes
    for event in notes:
        assert len(event["text"]) <= defaults.MAX_NOTE_RUNES
        assert event["text"].encode("utf-8").decode("utf-8") == event["text"]


async def test_every_event_validates_against_its_schema():
    _engine, outcome = await play(episode_steps=90)
    kinds = set()
    for turn in outcome.replay.turns:
        for event in turn["events"]:
            events.validate(event)
            kinds.add(event["k"])
    # A real episode of this game must at least mine, deposit, ram and lead.
    assert {"mine", "deposit", "collide", "lead", "spawn"} <= kinds, kinds


def test_the_event_vocabulary_is_complete_and_closed():
    for kind in ("spawn", "convert", "deposit", "mine", "collide", "yardraze",
                 "eliminate", "lead", "note", "fallback", "strike",
                 "budget_guard", "stop"):
        assert kind in events.EVENT_SCHEMA
    with pytest.raises(ValueError):
        events.validate({"k": "nonsense"})
    with pytest.raises(ValueError):
        events.validate({"k": "lead", "seat": 0})  # missing bank


async def test_per_turn_halite_is_integers_and_the_hash_pins_the_floats():
    _engine, outcome = await play(episode_steps=20)
    for turn in outcome.replay.turns:
        assert all(isinstance(v, int) for v in turn["halite"])
        assert len(turn["halite"]) == 21 * 21
        assert len(turn["hash"]) == 16


async def test_replay_size_is_about_one_megabyte_for_four_hundred_turns():
    _engine, outcome = await play(episode_steps=120, note=None)
    size = len(outcome.replay.to_bytes())
    per_turn = size / 120
    projected = per_turn * 400
    assert projected < 4_000_000, f"~{projected / 1e6:.1f} MB per 400 turns is too heavy"


# ---------------------------------------------------------- re-derivation
def rederive(doc: dict) -> None:
    """Replay ``seed`` + per-turn ``orders`` on a fresh sim and assert every
    recorded hash."""
    cfg = make_config(seed=doc["seed"], episode_steps=doc["config"]["episodeSteps"])
    sim = HaliteSim(cfg)
    sim.reset()
    stop_turn = doc["stop"]["turn"]
    for entry in doc["turns"]:
        assert sim.state_hash() == entry["hash"], (
            f"turn {entry['t']}: re-derived hash {sim.state_hash()} != recorded {entry['hash']}"
        )
        if entry["t"] >= stop_turn:
            break
        sim.step(entry["orders"])


async def test_rederivation_full_time():
    _engine, outcome = await play(episode_steps=40)
    assert outcome.end_rule == "full_time"
    rederive(outcome.replay.document())


async def test_rederivation_last_fleet():
    """Three seats march their opening ship onto one cell and mutually wreck.

    The four opening ships sit at (5,15), (15,15), (5,5) and (15,5); (10,10) is
    exactly 10 torus steps from each of the first three, so they arrive
    together with cargo 0 -- a tie for smallest cargo, which destroys every
    ship on the cell. None of them owns a shipyard, so all three are eliminated
    that turn and fewer than two seats remain active.
    """
    from cogame_halite import micro

    class Marcher(FakeLink):
        async def send(self, message):
            self.trace.append(("send", self.seat, message["turn"]))
            size = int(message["config"]["size"])
            target = micro.to_index(10, 10, size)
            actions = {}
            for sid, (pos, _cargo) in message["players"][self.seat][2].items():
                dirs = micro._shortest_dirs(int(pos), target, size)
                if dirs:
                    actions[sid] = dirs[0]
            self.queue.put_nowait(
                {"type": "orders", "turn": message["turn"], "actions": actions})

    links = [Marcher(i) for i in range(3)] + [FakeLink(3)]
    engine = Engine(make_config(episode_steps=200), links, sleep=nosleep)
    outcome = await engine.run()
    assert outcome.end_rule == "last_fleet", outcome.end_rule
    assert outcome.reason == "complete"
    assert outcome.replay.stop["rule"] == "last_fleet"
    assert outcome.results["eliminated_turn"][:3] == [10, 10, 10]
    assert outcome.results["scores"][:3] == [10 - 200 - 1] * 3
    rederive(outcome.replay.document())


async def test_rederivation_wall_clock():
    """A wall-clock stop is a wall-clock FACT: it cannot be recomputed from sim
    state (the particle-worlds 2026-08-26 scar), so it is RECORDED and applied
    by the same constructor on record and on re-derive."""
    _engine, outcome = await play(
        episode_steps=200, wall_clock_budget_seconds=660.0,
        budget_guard_seconds=600.0, clock=Clock(step=120.0))
    doc = outcome.replay.document()
    assert doc["stop"]["rule"] == "wall_clock"
    assert doc["results"]["reason"] == "deadline"
    rederive(doc)
    # Re-deriving must NOT invent a different stop: the recorded turn is the
    # only source.
    assert doc["stop"]["turn"] == doc["turns"][-1]["t"]


async def test_rederivation_fault(monkeypatch):
    from cogame_halite.sim import HaliteGuardError

    cfg = make_config(episode_steps=60)
    engine = Engine(cfg, [FakeLink(i) for i in range(4)], sleep=nosleep)
    real_step = engine.sim.step
    calls = {"n": 0}

    def exploding(orders):
        calls["n"] += 1
        if calls["n"] == 11:
            raise HaliteGuardError("synthetic fault")
        return real_step(orders)

    monkeypatch.setattr(engine.sim, "step", exploding)
    outcome = await engine.run()
    doc = outcome.replay.document()
    assert doc["stop"]["rule"] == "fault"
    assert doc["results"]["reason"] == "fault"
    rederive(doc)


async def test_a_replay_without_a_stop_record_cannot_be_written():
    _engine, outcome = await play(episode_steps=8)
    outcome.replay.stop = None
    with pytest.raises(ReplayError):
        outcome.replay.to_bytes()


def test_parse_rejects_a_foreign_document():
    with pytest.raises(ReplayError):
        parse(json.dumps({"format": "cogame-other-replay"}))
    with pytest.raises(ReplayError):
        parse(b"\xff\xfe not utf-8")
    with pytest.raises(ReplayError):
        parse("{not json")
