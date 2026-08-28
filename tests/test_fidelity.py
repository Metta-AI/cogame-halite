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
* **3 seeds x 399 turns** of the same comparison over a stream that
  deliberately **eliminates** a seat while its shipyard still stands — the
  elimination transcription is invisible to the random stream, which is built
  so no seat can ever be eliminated.
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
from fidelity_stream import (
    VICTIM_SEAT,
    elimination_stream_step,
    order_stream_step,
)

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "tests" / "upstream_reference.py"

#: The gate's floor. 8 seeds x 399 compared turns; a stream that ends early
#: fails ``test_gate_floor`` rather than passing quietly.
GATE_SEEDS = (1, 7, 42, 101, 2718, 31337, 65535, 999983)
GATE_TURNS = 399
BOARD_SEEDS = 50

#: Seeds for the ELIMINATION stream (``fidelity_stream.elimination_stream_step``),
#: which is the only part of the gate that can see the elimination
#: transcription: the random stream is built so no seat is ever eliminated.
ELIMINATION_SEEDS = (42, 2718, 999983)

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


def _our_run(seed: int, turns: int, step=order_stream_step) -> tuple[list[dict], list[list[dict]]]:
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
            "eliminated": list(sim.eliminated),
        }

    states = [snapshot()]
    stream: list[list[dict]] = []
    rng = random.Random(seed * 13 + 1)
    for _ in range(turns):
        orders = step(sim, rng)
        stream.append(orders)
        sim.step(orders)
        states.append(snapshot())
    return states, stream


def _reference(seed: int, stream: list[list[dict]]) -> list[dict]:
    return _run_reference(
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
    theirs = _reference(seed, stream)
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
@pytest.mark.parametrize("seed", ELIMINATION_SEEDS)
def test_differential_episode_with_an_elimination(seed: int):
    """The same exact-equality comparison over a stream that ELIMINATES a seat.

    The random stream is built so no seat can ever be eliminated, which leaves
    the elimination transcription (``sim.py::_eliminate``) outside the gate
    entirely. This stream bankrupts seat :data:`fidelity_stream.VICTIM_SEAT`
    down to no ships, a bank under the spawn cost and **a shipyard still
    standing**, then rams that abandoned yard.

    Upstream keeps a DONE agent's shipyards in ``obs.players`` (``halite.py``
    clears assets only for a status that is neither ACTIVE nor DONE), so the
    yard stays on the board as a razing hazard for everyone else. A port that
    clears it diverges here at the elimination turn.
    """
    ours, stream = _our_run(seed, GATE_TURNS, step=elimination_stream_step)
    theirs = _reference(seed, stream)
    assert len(theirs) == GATE_TURNS + 1, (
        f"seed {seed}: upstream stopped after {len(theirs) - 1} turns — only ONE "
        "seat may be eliminated, or the env ends early and the comparison shrinks"
    )
    _assert_identical(seed, ours, theirs)

    elimination_turn = ours[-1]["eliminated"][VICTIM_SEAT]
    assert elimination_turn is not None, (
        f"seed {seed}: the elimination stream eliminated nobody — it is no "
        "longer covering the elimination transcription"
    )
    # The yard outlives the seat, and upstream says so too.
    assert ours[elimination_turn]["players"][VICTIM_SEAT][1], (
        f"seed {seed}: the victim was eliminated with no shipyard, which is the "
        "case that cannot tell the two behaviours apart"
    )
    assert theirs[elimination_turn]["status"][VICTIM_SEAT] == "DONE"
    assert theirs[elimination_turn]["reward"][VICTIM_SEAT] == (
        elimination_turn - defaults.EPISODE_STEPS - 1
    )
    # ... and is razed later by the raider, which can only happen if it stood.
    razed = [
        turn
        for turn in range(elimination_turn, len(ours))
        if not ours[turn]["players"][VICTIM_SEAT][1]
    ]
    assert razed, f"seed {seed}: the abandoned yard was never razed"


def test_the_elimination_stream_eliminates_one_seat_with_a_yard_standing():
    """The property the differential case rests on, without needing upstream."""
    for seed in ELIMINATION_SEEDS:
        sim = HaliteSim(make_config(seed=seed, episode_steps=defaults.EPISODE_STEPS))
        sim.reset()
        rng = random.Random(seed * 13 + 1)
        standing = None
        for _ in range(GATE_TURNS):
            sim.step(elimination_stream_step(sim, rng))
            if sim.eliminated[VICTIM_SEAT] is not None and standing is None:
                standing = dict(sim.players[VICTIM_SEAT][1])
        assert sim.turn == GATE_TURNS, f"seed {seed}: reached turn {sim.turn}"
        assert sim.eliminated[VICTIM_SEAT] is not None, f"seed {seed}: nobody was eliminated"
        assert standing, f"seed {seed}: eliminated with no shipyard standing"
        others = [e for s, e in enumerate(sim.eliminated) if s != VICTIM_SEAT]
        assert others == [None] * (defaults.NUM_SEATS - 1), (
            f"seed {seed}: a second seat was eliminated ({sim.eliminated}) — the "
            "upstream env would end the episode early and shrink the gate"
        )
        assert not sim.players[VICTIM_SEAT][1], (
            f"seed {seed}: the abandoned yard was never razed"
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
    assert len(ELIMINATION_SEEDS) >= 3, (
        "the elimination stream is the only part of the gate that compares an "
        "eliminated seat's assets"
    )


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
