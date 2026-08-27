"""Test fixtures shared by the cogame-halite suite."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT / "server"), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from cogame_halite import micro  # noqa: E402
from cogame_halite.config import GameConfig  # noqa: E402
from cogame_halite.sim import HaliteSim  # noqa: E402

NAMES = ["daveey", "daveey-1", "halite-tidewalker", "halite-corsair"]


def make_config(**overrides) -> GameConfig:
    data = {
        "players": [{"name": n} for n in NAMES],
        "tokens": [f"token-{i}" for i in range(4)],
        "seed": 42,
        "episode_steps": 60,
    }
    data.update(overrides)
    return GameConfig.from_dict(data)


def make_sim(**overrides) -> HaliteSim:
    sim = HaliteSim(make_config(**overrides))
    sim.reset()
    return sim


def play_scripted(sim: HaliteSim, turns: int, baselines=("tidewalker", "corsair", "tidewalker", "corsair")):
    """Drive a sim with the bundled baselines. Returns the per-turn orders."""
    stream = []
    for _ in range(turns):
        view = micro.BoardView.from_sim(sim)
        orders = [
            micro.compile_turn(view, seat, micro.baseline_directive(baselines[seat]),
                               baseline=baselines[seat])
            for seat in range(sim.num_seats)
        ]
        stream.append(orders)
        sim.step(orders)
    return stream


class FakeLink:
    """An in-process seat transport for the engine tests.

    Records the exact order of ``send`` and ``receive`` calls so the
    parallel-batch property (every ``observe`` frame written before any reply
    is awaited) can be asserted.
    """

    def __init__(self, seat: int, *, baseline: str = "tidewalker", trace: list | None = None,
                 behaviour: str = "ok", note: str | None = None):
        self.seat = seat
        self.baseline = baseline
        self.connected = True
        self.trace = trace if trace is not None else []
        self.behaviour = behaviour
        self.note = note
        self.directive = micro.baseline_directive(baseline)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent = 0

    async def send(self, message: dict) -> None:
        self.trace.append(("send", self.seat, message["turn"]))
        self.sent += 1
        if self.behaviour == "silent":
            return
        if self.behaviour == "malformed":
            self.queue.put_nowait({"type": "garbage"})
            return
        if self.behaviour == "wrong_turn":
            self.queue.put_nowait({"type": "orders", "turn": message["turn"] + 7, "actions": {}})
            return
        if self.behaviour == "disconnected":
            self.queue.put_nowait(None)
            return
        view = micro.BoardView.from_observation(message)
        reply = {
            "type": "orders",
            "turn": message["turn"],
            "source": "llm" if message.get("directive") else "scripted",
            "actions": micro.compile_turn(view, self.seat, self.directive, baseline=self.baseline),
        }
        if self.note:
            reply["note"] = self.note
        self.queue.put_nowait(reply)

    async def receive(self):
        self.trace.append(("receive", self.seat))
        return await self.queue.get()


@pytest.fixture
def config() -> GameConfig:
    return make_config()


@pytest.fixture
def sim() -> HaliteSim:
    return make_sim()
