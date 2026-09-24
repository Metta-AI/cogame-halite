"""Check complete, seed-separated Halite post-training games."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from cogame_halite import defaults, micro
from players import llm


for variant in ("standard", "sprint", "richfields"):
    with TemporaryDirectory() as temporary:
        output = Path(temporary) / variant
        subprocess.run([sys.executable, "tools/export_posttrain.py", str(output), "10", variant], check=True)
        manifest = json.loads((output / "manifest.json").read_text())
        train = [json.loads(line) for line in (output / "train.jsonl").read_text().splitlines()]
        validation = [json.loads(line) for line in (output / "validation.jsonl").read_text().splitlines()]
        assert len(manifest["runs"]) == 10
        assert manifest["train_examples"] == len(train) > 0
        assert manifest["validation_examples"] == len(validation) > 0
        assert {row["seed"] for row in train}.isdisjoint({row["seed"] for row in validation})
        assert all(run["end_rule"] in ("full_time", "last_fleet") for run in manifest["runs"])
        for row in train + validation:
            assert row["game"] == "halite"
            assert [part["role"] for part in row["prompt"]] == ["system", "user"]
            assert row["prompt"][0]["content"] == llm.SYSTEM_PROMPT
            assert "STANDINGS" in row["prompt"][1]["content"]
            directive = json.loads(row["completion"][0]["content"])
            assert llm.repair(directive, micro.TIDEWALKER, list(defaults.ALIASES), 0, 400).as_dict() == directive
        print(f"{variant}: {len(train)} train, {len(validation)} validation decisions")
