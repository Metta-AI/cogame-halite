"""2. The fidelity gate — the acceptance criterion for the whole port.

`AGENTS.md` rule 2: **if this fails, the port is wrong, never the test.**
Weakening or skipping it is a failed task, not a passing build.

What it proves, against a real ``kaggle-environments==1.32.7`` install:

* **8 seeds x 399 turns** of exact-equality differential comparison — the
  441-entry ``halite`` list element for element (exact floats), every player's
  ``[bank, shipyards, ships]`` **including dict insertion order**, ``step``,
  and each agent's ``status``/``reward``.
* **50 seeds** of board generation, cell for cell, with the four starting
  positions exactly ``[110, 120, 320, 330]``.
* A **tick-count floor**, so a shrunken order stream can never quietly weaken
  the gate.

The upstream env runs in a subprocess with a clean ``sys.path``
(``tests/upstream_reference.py``): the assembled vendor tree is a package with
the same name, so the two cannot coexist in one interpreter.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import make_config  # noqa: E402  (tests/ is on sys.path)
from cogame_halite import defaults
from cogame_halite.sim import HaliteSim
from fidelity_stream import order_stream_step

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "tests" / "upstream_reference.py"

#: The gate's floor. 8 seeds x 399 compared turns; a stream that ends early
#: fails ``test_gate_floor`` rather than passing quietly.
GATE_SEEDS = (1, 7, 42, 101, 2718, 31337, 65535, 999983)
GATE_TURNS = 399
BOARD_SEEDS = 50

pytestmark = pytest.mark.fidelity

_upstream_available = importlib.util.find_spec("kaggle_environments") is not None
requires_upstream = pytest.mark.skipif(
    not _upstream_available,
    reason=(
        "kaggle-environments is not installed. It is the CI-only `fidelity` "
        "dependency group: `uv sync --frozen --group fidelity`. CI ALWAYS runs "
        "it (.github/workflows/ci.yml), so a local skip is never a green gate."
    ),
)


def _run_reference(payload: dict) -> list[dict]:
    """Drive upstream's env in a clean-path subprocess."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as work:
        inp = Path(work) / "in.json"
        out = Path(work) / "out.json"
        inp.write_text(json.dumps(payload))
        result = subprocess.run(
            [sys.executable, str(REFERENCE), str(inp), str(out)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            raise AssertionError(
                "the upstream reference subprocess failed:\n"
                f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}"
            )
        return json.loads(out.read_text())


def _our_run(seed: int, turns: int) -> tuple[list[dict], list[list[dict]]]:
    sim = HaliteSim(make_config(seed=seed, episode_steps=defaults.EPISODE_STEPS))
    sim.reset()

    def snapshot() -> dict:
        return {
            "step": sim.turn,
            "halite": list(sim.halite),
            "players": [
                [p[0], dict(p[1]), {k: list(v) for k, v in p[2].items()}]
                for p in sim.players
            ],
        }

    states = [snapshot()]
    stream: list[list[dict]] = []
    rng = random.Random(seed * 13 + 1)
    for _ in range(turns):
        orders = order_stream_step(sim, rng)
        stream.append(orders)
        sim.step(orders)
        states.append(snapshot())
    return states, stream


def _assert_identical(seed: int, ours: list[dict], theirs: list[dict]) -> None:
    assert len(ours) == len(theirs), (
        f"seed {seed}: we recorded {len(ours)} states, upstream {len(theirs)} — "
        "the episode diverged in length"
    )
    for turn, (mine, up) in enumerate(zip(ours, theirs)):
        assert mine["step"] == up["step"], f"seed {seed} turn {turn}: step"
        # Exact float equality, element for element.
        assert mine["halite"] == up["halite"], (
            f"seed {seed} turn {turn}: the 441-cell halite array diverged at "
            f"index {next(i for i, (a, b) in enumerate(zip(mine['halite'], up['halite'])) if a != b)}"
        )
        for seat, (a, b) in enumerate(zip(mine["players"], up["players"])):
            assert a[0] == b[0], f"seed {seed} turn {turn} seat {seat}: bank {a[0]} != {b[0]}"
            # Dict INSERTION ORDER is part of the contract: it is the order
            # spawns and converts are processed in.
            assert list(a[1].items()) == list(b[1].items()), (
                f"seed {seed} turn {turn} seat {seat}: shipyards (or their order) diverged"
            )
            assert list(a[2].items()) == list(b[2].items()), (
                f"seed {seed} turn {turn} seat {seat}: ships (or their order) diverged"
            )


@requires_upstream
@pytest.mark.parametrize("seed", GATE_SEEDS)
def test_differential_episode(seed: int):
    """One seed, 399 turns, exact equality at every turn."""
    ours, stream = _our_run(seed, GATE_TURNS)
    theirs = _run_reference(
        {
            "configuration": {
                "size": defaults.BOARD_SIZE,
                "episodeSteps": defaults.EPISODE_STEPS,
                "startingHalite": defaults.STARTING_HALITE,
                "randomSeed": seed,
            },
            "num_agents": defaults.NUM_SEATS,
            "orders": stream,
        }
    )
    assert len(theirs) == GATE_TURNS + 1, (
        f"seed {seed}: upstream stopped after {len(theirs) - 1} turns — the order "
        "stream must keep every seat alive so the gate compares the full episode"
    )
    _assert_identical(seed, ours, theirs)

    # Statuses and rewards, too. With no seat eliminated every agent is ACTIVE
    # until the env itself runs out of steps.
    final = theirs[-1]
    sim = HaliteSim(make_config(seed=seed, episode_steps=defaults.EPISODE_STEPS))
    sim.reset()
    for orders in stream:
        sim.step(orders)
    assert sim.eliminated == [None] * defaults.NUM_SEATS
    assert final["reward"] == sim.banks(), (
        f"seed {seed}: upstream rewards {final['reward']} != our banks {sim.banks()}"
    )


@requires_upstream
def test_board_generation_matches_for_fifty_seeds():
    for seed in range(BOARD_SEEDS):
        theirs = _run_reference(
            {
                "configuration": {
                    "size": defaults.BOARD_SIZE,
                    "episodeSteps": defaults.EPISODE_STEPS,
                    "startingHalite": defaults.STARTING_HALITE,
                    "randomSeed": seed,
                },
                "num_agents": defaults.NUM_SEATS,
                "orders": [],
            }
        )[0]
        sim = HaliteSim(make_config(seed=seed))
        sim.reset()
        assert sim.halite == theirs["halite"], f"seed {seed}: generated board diverged"
        ours = [
            [p[0], dict(p[1]), {k: list(v) for k, v in p[2].items()}] for p in sim.players
        ]
        assert ours == theirs["players"], f"seed {seed}: opening assets diverged"
        starts = [list(p[2].values())[0][0] for p in sim.players]
        assert starts == list(defaults.STARTING_POSITIONS), (
            f"seed {seed}: starting positions {starts}"
        )


def test_gate_floor():
    """The gate compares at least 8 x 399 turns. Shrinking it is a failure."""
    assert len(GATE_SEEDS) >= 8
    assert GATE_TURNS >= 399
    assert len(GATE_SEEDS) * GATE_TURNS >= 8 * 399
    assert BOARD_SEEDS >= 50


def test_the_order_stream_keeps_every_seat_alive():
    """A stream that eliminates a seat ends the upstream env early and would
    silently shrink the gate. Prove it does not, without needing upstream."""
    for seed in GATE_SEEDS:
        sim = HaliteSim(make_config(seed=seed, episode_steps=defaults.EPISODE_STEPS))
        sim.reset()
        rng = random.Random(seed * 13 + 1)
        for _ in range(GATE_TURNS):
            sim.step(order_stream_step(sim, rng))
        assert sim.turn == GATE_TURNS, f"seed {seed}: reached turn {sim.turn}"
        assert sim.eliminated == [None] * defaults.NUM_SEATS, (
            f"seed {seed}: a seat was eliminated at {sim.eliminated}"
        )
