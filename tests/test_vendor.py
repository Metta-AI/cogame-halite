"""1. Vendor identity — the re-vendor tripwire.

`vendor/upstream/` is byte-pristine (`AGENTS.md` rule 1). This test re-derives
every sha256 in `vendor/UPSTREAM.md` from the files themselves, byte-compares
the assembled tree against the vendor originals, and re-reads every rule
constant the design note names from the **vendored** `halite.json`. A re-vendor
that changes `collectRate` fails here, loudly, instead of silently changing the
game.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from cogame_halite import defaults

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "vendor" / "upstream"
UPSTREAM_MD = REPO / "vendor" / "UPSTREAM.md"
PATCHES_MD = REPO / "vendor" / "PATCHES.md"

VENDORED_FILES = (
    "kaggle_environments/helpers.py",
    "kaggle_environments/envs/halite/helpers.py",
    "kaggle_environments/envs/halite/halite.py",
    "kaggle_environments/envs/halite/halite.json",
)


def _recorded_hashes() -> dict[str, str]:
    rows = re.findall(
        r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|$",
        UPSTREAM_MD.read_text(encoding="utf-8"),
        flags=re.M,
    )
    return dict(rows)


def test_upstream_md_lists_every_vendored_file():
    recorded = _recorded_hashes()
    assert set(recorded) == set(VENDORED_FILES), (
        "vendor/UPSTREAM.md's sha256 table and the vendored tree disagree"
    )


@pytest.mark.parametrize("rel", VENDORED_FILES)
def test_vendored_file_matches_its_recorded_sha256(rel: str):
    path = VENDOR / rel
    assert path.is_file(), f"vendored file missing: {path}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == _recorded_hashes()[rel], (
        f"{rel} does not match vendor/UPSTREAM.md — vendor/upstream is byte-pristine"
    )


def test_upstream_md_records_the_release_and_commit():
    text = UPSTREAM_MD.read_text(encoding="utf-8")
    assert "kaggle-environments==1.32.7" in text
    assert re.search(r"`[0-9a-f]{40}`", text), "no commit hash recorded"


def test_patches_md_says_zero_patches():
    text = PATCHES_MD.read_text(encoding="utf-8")
    assert "Zero patches." in text
    # The two allowed transcriptions must stay named, with their citations.
    assert "populate_board" in text
    assert "interpreter" in text


def test_assembled_tree_is_byte_identical_except_the_three_shims():
    from sim.assemble import DEFAULT_OUT, SHIM_FILES, VENDOR_FILES, assemble

    out = assemble(DEFAULT_OUT)
    for rel in VENDOR_FILES:
        assert (out / rel).read_bytes() == (VENDOR / rel).read_bytes(), rel
    for rel in SHIM_FILES:
        assert (out / rel).is_file(), rel
    assembled = {
        str(p.relative_to(out)).replace("\\", "/")
        for p in out.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert assembled == set(VENDOR_FILES) | set(SHIM_FILES), (
        "the assembled tree carries files that are neither vendor nor shim"
    )


CONSTANT_MIRROR = {
    "size": ("BOARD_SIZE", 21),
    "episodeSteps": ("EPISODE_STEPS", 400),
    "startingHalite": ("STARTING_HALITE", 24000),
    "spawnCost": ("SPAWN_COST", 500),
    "convertCost": ("CONVERT_COST", 500),
    "moveCost": ("MOVE_COST", 0),
    "collectRate": ("COLLECT_RATE", 0.25),
    "regenRate": ("REGEN_RATE", 0.02),
    "maxCellHalite": ("MAX_CELL_HALITE", 500),
}


@pytest.mark.parametrize("key,mirror", sorted(CONSTANT_MIRROR.items()))
def test_rule_constants_are_read_from_the_vendored_json(key, mirror):
    name, expected = mirror
    upstream = defaults.upstream_defaults()[key]
    assert upstream == expected, f"vendored halite.json changed {key}: {upstream}"
    assert getattr(defaults, name) == upstream, (
        f"defaults.{name} drifted from the vendored halite.json"
    )


def test_reward_default_is_the_opening_bank():
    spec = defaults.upstream_spec()
    assert spec["reward"]["default"] == defaults.STARTING_BANK == 5000


def test_action_enum_matches_the_vendored_spec():
    spec = defaults.upstream_spec()
    enum = spec["action"]["additionalProperties"]["enum"]
    assert sorted(enum) == sorted(defaults.ALL_ACTIONS)


def test_agent_counts_include_four():
    assert defaults.NUM_SEATS in defaults.upstream_spec()["agents"]


def test_spec_is_halite_four():
    spec = defaults.upstream_spec()
    assert spec["name"] == "halite"
    assert spec["title"] == "Halite 4"
    assert spec["version"] == "1.2.1"
