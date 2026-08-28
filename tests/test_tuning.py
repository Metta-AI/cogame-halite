"""The baseline constants are the grid harness's choice, not a guess.

Checklist item 7's second sentence — "The baseline's parameters were tuned with
a grid harness, not guessed" — has two artefacts: `tools/tune/grid_search.py`
(the harness) and `docs/tuning/2026-08-28-micro-grid.md` (the run that chose
them). This file ties both to the shipped constants: the recorded winner must
still be what `micro.py` ships, and the harness must still run.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from cogame_halite import micro

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tools" / "tune" / "grid_search.py"
RECORD = REPO / "docs" / "tuning" / "2026-08-28-micro-grid.md"


def _load_harness():
    if "grid_search" in sys.modules:
        return sys.modules["grid_search"]
    spec = importlib.util.spec_from_file_location("grid_search", HARNESS)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves `cls.__module__` through sys.modules, so the module
    # has to be registered before its body runs.
    sys.modules["grid_search"] = module
    spec.loader.exec_module(module)
    return module


def _chosen() -> dict[str, dict[str, int]]:
    """The `## Chosen` block of the recorded run."""
    block = RECORD.read_text().split("## Chosen", 1)[1].split("```")[1]
    out: dict[str, dict[str, int]] = {}
    for line in block.strip().splitlines():
        name, _, rest = line.strip().partition(" ")
        out[name] = {
            key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", rest)
        }
    return out


def test_the_shipped_constants_are_the_recorded_sweep_winners():
    chosen = _chosen()
    assert set(chosen) == {"tidewalker", "corsair"}
    for name, fields in chosen.items():
        directive = micro.baseline_directive(name)
        for field, value in fields.items():
            assert getattr(directive, field) == value, (
                f"{name}.{field} is {getattr(directive, field)}, but "
                f"{RECORD.name} chose {value}. Re-run the sweep and update both, "
                "or put the winner back."
            )
    # The turn-0 directive every LLM seat starts from IS tidewalker.
    assert micro.Directive() == micro.TIDEWALKER


def test_the_record_shows_the_runoff_beat_the_constants_it_replaced():
    text = RECORD.read_text()
    assert "16 fresh seeds" in text and "RUNOFF_SEEDS" in text
    assert "Stage 1 — the grid" in text and "Stage 2 — the runoff" in text
    # Every grid axis the harness sweeps has a column in the recorded tables.
    harness = _load_harness()
    for axis in harness.GRID:
        assert f"`{axis}`" in text, f"the record has no column for {axis}"
    assert len(harness.RUNOFF_SEEDS) >= 16
    assert not set(harness.RUNOFF_SEEDS) & set(harness.SEEDS), (
        "the runoff must be out of sample"
    )


def test_the_harness_still_runs():
    """A real (tiny) sweep: two candidates, one seed, a short episode. If the
    harness stops driving the shipped compile_turn, this is where it shows."""
    harness = _load_harness()
    grid = {"mineFloor": (100, 200), "returnAt": (300,), "spawnUntil": (200,)}
    outcomes = harness.sweep(
        "tidewalker", grid, harness.SEEDS[:1], 40, micro.TIDEWALKER
    )
    assert len(outcomes) == 2
    assert all(o.episodes == 1 for o in outcomes)
    assert outcomes == sorted(outcomes, key=lambda o: (-o.mean_margin, -o.mean_score))
    table = harness.table("tidewalker", outcomes)
    assert table.count("|") > 8 and "win rate" in table
