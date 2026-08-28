#!/usr/bin/env python3
"""Grid harness for the scripted baselines' constants.

The baselines are `tidewalker` (also the server-side fallback and the turn-0
directive every LLM seat starts from) and `corsair`. Their constants —
`mineFloor`, `returnAt`, `spawnUntil` — are not guesses: they are the winners
of the sweep this script runs, and `docs/tuning/2026-08-28-micro-grid.md`
records the run that chose them.

The sweep is honest for one reason the game gives us for free: `populate_board`
mirrors one 11 x 11 quartile into all four quadrants, so the board is exactly
4-fold symmetric and the four seats are equivalent by construction. A candidate
seated at seat 0 against the shipped baselines therefore needs no seat rotation
to be fair — only a spread of seeds.

    # the full sweep (slow: combos x seeds full-length episodes)
    python tools/tune/grid_search.py --out docs/tuning/2026-08-28-micro-grid.md

    # what the test runs: tiny grid, few seeds, short episodes
    python tools/tune/grid_search.py --quick

Every episode is a real `HaliteSim` episode driven by the shipped
`micro.compile_turn`, so the harness cannot drift from what the game plays.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for entry in (str(REPO / "server"), str(REPO)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from cogame_halite import micro  # noqa: E402
from cogame_halite.config import GameConfig  # noqa: E402
from cogame_halite.results import score_of  # noqa: E402
from cogame_halite.sim import HaliteSim  # noqa: E402

#: The swept axes. Each is a directive field `micro.compile_turn` reads.
GRID: dict[str, tuple[int, ...]] = {
    "mineFloor": (50, 100, 150, 200),
    "returnAt": (300, 350, 500, 700),
    "spawnUntil": (200, 300, 340),
}
QUICK_GRID: dict[str, tuple[int, ...]] = {
    "mineFloor": (100, 200),
    "returnAt": (350, 500),
    "spawnUntil": (300,),
}

#: Who the candidate plays against. Seat 0 is the candidate; the other three
#: are the shipped baselines, so the incumbent is always in the room.
OPPONENTS = ("corsair", "tidewalker", "corsair")

SEEDS = (1, 7, 42, 101, 2718, 31337, 65535, 999983)

#: Fresh seeds for the runoff. Picking the maximum of a 48-cell grid measured
#: on six episodes is overfitting; the top few are replayed here on seeds the
#: grid never saw, and THAT is what chooses the shipped constants.
RUNOFF_SEEDS = (
    3, 11, 23, 59, 97, 137, 211, 307,
    401, 523, 617, 719, 811, 907, 1013, 1109,
)


@dataclass(frozen=True)
class Outcome:
    candidate: dict[str, int]
    wins: int
    episodes: int
    mean_score: float
    mean_margin: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.episodes if self.episodes else 0.0


def play(candidate: micro.Directive, baseline: str, seed: int, turns: int) -> list[int]:
    """One all-scripted episode. Returns each seat's score."""
    config = GameConfig.from_dict(
        {
            "players": [{"name": f"seat-{i}"} for i in range(4)],
            "tokens": [f"token-{i}" for i in range(4)],
            "seed": seed,
            "episode_steps": turns,
        }
    )
    sim = HaliteSim(config)
    sim.reset()
    directives = [candidate] + [micro.baseline_directive(n) for n in OPPONENTS]
    baselines = [baseline, *OPPONENTS]
    for _ in range(turns - 1):
        view = micro.BoardView.from_sim(sim)
        orders = [
            micro.compile_turn(view, seat, directives[seat], baseline=baselines[seat])
            for seat in range(4)
        ]
        result = sim.step(orders)
        if result.last_fleet:
            break
    return [
        score_of(bank, sim.eliminated[seat], config.episode_steps)
        for seat, bank in enumerate(sim.banks())
    ]


def sweep(baseline: str, grid: dict[str, tuple[int, ...]], seeds, turns: int,
          base: micro.Directive) -> list[Outcome]:
    outcomes: list[Outcome] = []
    only = grid.get("__only__")
    axes = sorted(axis for axis in grid if axis != "__only__")
    for values in itertools.product(*(grid[axis] for axis in axes)):
        if only is not None and values not in only:
            continue
        candidate = dict(zip(axes, values))
        directive = micro.Directive(
            **{**base.as_dict(), **candidate}
        )
        wins = 0
        total = 0.0
        margin = 0.0
        for seed in seeds:
            scores = play(directive, baseline, seed, turns)
            best_other = max(scores[1:])
            if scores[0] >= max(scores):
                wins += 1
            total += scores[0]
            margin += scores[0] - best_other
        outcomes.append(
            Outcome(
                candidate=candidate,
                wins=wins,
                episodes=len(seeds),
                mean_score=total / len(seeds),
                mean_margin=margin / len(seeds),
            )
        )
        print(
            f"  {baseline} {candidate} -> wins {wins}/{len(seeds)} "
            f"mean score {total / len(seeds):.0f} margin {margin / len(seeds):+.0f}",
            file=sys.stderr,
        )
    return sorted(outcomes, key=lambda o: (-o.mean_margin, -o.mean_score))


def candidates_grid(spec: str) -> dict[str, tuple[int, ...]]:
    """An explicit candidate list, expressed as a grid the sweep can walk.

    ``--only`` takes ``field=value,field=value;field=value,...``; the sweep
    below is a product over the axes, so the cross product is filtered down to
    exactly the listed combinations by :func:`sweep`.
    """
    combos = []
    for chunk in spec.split(";"):
        if not chunk.strip():
            continue
        combos.append(
            {
                key.strip(): int(value)
                for key, value in (part.split("=") for part in chunk.split(","))
            }
        )
    axes = sorted({key for combo in combos for key in combo})
    out = {axis: tuple(sorted({combo[axis] for combo in combos})) for axis in axes}
    out["__only__"] = tuple(  # type: ignore[assignment]
        tuple(combo[axis] for axis in axes) for combo in combos
    )
    return out


def table(baseline: str, outcomes: list[Outcome]) -> str:
    axes = sorted(outcomes[0].candidate)
    head = " | ".join(f"`{axis}`" for axis in axes)
    lines = [
        f"### `{baseline}`",
        "",
        f"| {head} | win rate | mean score | mean margin over the best rival |",
        "|" + "---|" * (len(axes) + 3),
    ]
    for outcome in outcomes:
        cells = " | ".join(str(outcome.candidate[axis]) for axis in axes)
        lines.append(
            f"| {cells} | {outcome.wins}/{outcome.episodes} | "
            f"{outcome.mean_score:.0f} | {outcome.mean_margin:+.0f} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=400)
    parser.add_argument("--seeds", type=int, default=len(SEEDS))
    parser.add_argument("--quick", action="store_true", help="the tiny grid the test runs")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--baseline", choices=("tidewalker", "corsair", "both"), default="both"
    )
    parser.add_argument(
        "--only",
        default="",
        help="run these candidates instead of the grid, e.g. "
        "'mineFloor=200,returnAt=300,spawnUntil=200;mineFloor=100,...'",
    )
    parser.add_argument(
        "--runoff",
        action="store_true",
        help="use RUNOFF_SEEDS (fresh seeds the grid never saw)",
    )
    args = parser.parse_args(argv)

    grid = QUICK_GRID if args.quick else GRID
    if args.only:
        grid = candidates_grid(args.only)
    turns = 60 if args.quick else args.turns
    pool = RUNOFF_SEEDS if args.runoff else SEEDS
    seeds = pool[: 2 if args.quick else args.seeds]
    names = ("tidewalker", "corsair") if args.baseline == "both" else (args.baseline,)

    chunks = []
    for baseline in names:
        base = micro.baseline_directive(baseline)
        outcomes = sweep(baseline, grid, seeds, turns, base)
        best = outcomes[0]
        shipped = {
            axis: getattr(base, axis) for axis in sorted(grid) if axis != "__only__"
        }
        print(
            f"{baseline}: best {best.candidate} (margin {best.mean_margin:+.0f}), "
            f"shipped {shipped}",
            file=sys.stderr,
        )
        chunks.append(table(baseline, outcomes))
        chunks.append(
            f"Best by mean margin: `{best.candidate}`; shipped: `{shipped}`.\n"
        )

    body = "\n".join(chunks)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
