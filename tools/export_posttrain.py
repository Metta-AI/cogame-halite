"""Export complete native Halite games as hosted directive conversations."""

import argparse
import json
import subprocess
from pathlib import Path

from cogame_halite import defaults, micro, results
from cogame_halite.config import GameConfig
from cogame_halite.engine import Engine
from players import llm


def export(output: Path, episodes: int, variant: str) -> None:
    assert episodes >= 10
    assert not output.exists()
    manifest = json.loads(Path("coworld_manifest_template.json").read_text())
    variant_config = next(entry["game_config"] for entry in manifest["variants"] if entry["id"] == variant)
    output.mkdir()
    splits = {"train": [], "validation": []}
    runs = []
    for seed in range(1, episodes + 1):
        config = GameConfig.from_dict({**variant_config, "seed": seed, "tokens": [f"training-{seat}" for seat in range(4)]})
        engine = Engine(config, [None] * 4)
        sim = engine.sim
        sim.reset()
        baselines = ["tidewalker" if (seat + seed) % 2 == 0 else "corsair" for seat in range(4)]
        directives = [
            llm.repair(
                micro.baseline_directive(name).as_dict(), micro.TIDEWALKER,
                list(defaults.ALIASES), seat, config.episode_steps,
            )
            for seat, name in enumerate(baselines)
        ]
        previous = [None] * 4
        rows = []
        while sim.turn < config.episode_steps - 1:
            turn = sim.turn
            if turn % config.directive_every == 0:
                for seat in range(4):
                    observation = engine._observation(seat, turn, True, config.directive_deadline_ms)
                    completion = directives[seat].as_dict()
                    assert llm.repair(completion, directives[seat], list(defaults.ALIASES), seat, config.episode_steps) == directives[seat]
                    rows.append(json.dumps({
                        "episode_id": f"halite-{variant}-{seed}", "seed": f"halite-{variant}-{seed}",
                        "decision_id": len(rows),
                        "prompt": [
                            {"role": "system", "content": llm.SYSTEM_PROMPT},
                            {"role": "user", "content": llm.build_prompt(observation, "", previous[seat])},
                        ],
                        "completion": [{"role": "assistant", "content": json.dumps(completion, separators=(",", ":"))}],
                        "game": "halite", "action_schema_revision": "halite-directive-v1",
                    }, separators=(",", ":")))
                    previous[seat] = directives[seat]
            view = micro.BoardView.from_sim(sim)
            orders = [micro.compile_turn(view, seat, directives[seat], baseline=baselines[seat]) for seat in range(4)]
            result = sim.step(orders)
            if result.last_fleet:
                break
        scores = [results.score_of(int(sim.players[seat][0]), sim.eliminated[seat], config.episode_steps) for seat in range(4)]
        splits["validation" if seed % 5 == 0 else "train"].extend(rows)
        runs.append({"seed": seed, "turns": sim.turn, "decisions": len(rows), "scores": scores, "end_rule": "last_fleet" if result.last_fleet else "full_time"})
    for split, rows in splits.items():
        (output / f"{split}.jsonl").write_text("\n".join(rows) + "\n")
    (output / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "game": "halite", "variant": variant,
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "teacher": "tidewalker-and-corsair", "train_examples": len(splits["train"]),
        "validation_examples": len(splits["validation"]), "runs": runs,
    }, indent=2) + "\n")
    print(f"{variant} train={len(splits['train'])} validation={len(splits['validation'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("episodes", type=int)
    parser.add_argument("variant")
    args = parser.parse_args()
    export(args.output, args.episodes, args.variant)
