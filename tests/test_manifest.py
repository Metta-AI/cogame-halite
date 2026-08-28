"""12. Manifest — the platform contract, pinned where it has bitten before.

Every assertion here corresponds to a documented upload/certify failure. The
last test runs the **installed** ``coworld`` CLI's own
``_load_template_manifest`` + ``validate_upload_manifest``, which is the only
thing that catches a pydantic contract change before phase 40 does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cogame_halite import defaults
from cogame_halite.version import GAME_VERSION

REPO = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO / "coworld_manifest_template.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())
IMAGE_PLACEHOLDER = "{{HALITE_IMAGE}}"


# ------------------------------------------------------------- top level
def test_top_level_shape():
    assert MANIFEST["$schema"].startswith("https://")
    assert len(MANIFEST["tags"]) >= 3
    assert MANIFEST["episode_timeout_minutes"] == defaults.PLATFORM_EPISODE_TIMEOUT_MINUTES == 20
    assert "version" not in MANIFEST, "a top-level version is rejected by coworld 0.1.42+"


def test_game_block_shape():
    game = MANIFEST["game"]
    assert game["name"] == "halite", "the secret namespace is game.name"
    assert "_" not in game["name"], "game.name and the page slug must agree"
    assert game["replay_viewer"] == {"bundle": "static-replay-viewer"}, (
        "replay_viewer lives UNDER game, and replays are a static bundle, never a pod"
    )
    assert "replay_viewer" not in MANIFEST, "not at the top level"
    assert game["description"], "game.description is required"
    assert game["owner"], "game.owner is required"
    assert "display_name" not in game, "game.display_name is rejected"
    assert "tags" not in game, "tags live at the top level only"


def test_the_runnable_is_a_game_on_the_compose_image():
    runnable = MANIFEST["game"]["runnable"]
    assert runnable["type"] == "game"
    assert runnable["image"] == IMAGE_PLACEHOLDER
    assert runnable["run"] == ["python", "-m", "cogame_halite.server"]
    assert runnable["source_url"].startswith("https://github.com/Metta-AI/cogame-halite")


def test_the_image_placeholder_is_derived_from_the_compose_service_name():
    """`coworld build` derives placeholders from compose service names:
    `service halite` -> {{HALITE_IMAGE}}. {{GAME_IMAGE}} is not a thing
    (the lantern 0.1.0 scar)."""
    compose = (REPO / "compose.yaml").read_text()
    assert re.search(r"^\s{2}halite:\s*$", compose, flags=re.M), "compose service must be `halite`"
    assert "image: coworld-halite:latest" in compose
    assert "platform: linux/amd64" in compose
    assert "network: host" in compose
    blob = json.dumps(MANIFEST)
    assert "{{GAME_IMAGE}}" not in blob and "{{PLAYER_IMAGE}}" not in blob
    for image in re.findall(r"\{\{[A-Z_]+\}\}", blob):
        assert image == IMAGE_PLACEHOLDER


#: Which repo file each inline doc carries. The platform renders `game.docs`
#: itself, so the docs are INLINE TEXT (`{"type":"text","value":...}`) rather
#: than a `blob` URL a reader would have to follow; the test below is what
#: keeps the two copies identical.
DOC_SOURCES = {
    ("readme",): "README.md",
    ("pages", "rules.md"): "docs/RULES.md",
    ("pages", "replay.md"): "docs/REPLAY.md",
}


def test_docs_and_protocols_are_object_shaped():
    """game.protocols.player/.global (like game.docs.readme) must be
    {"type":..., "value":...} objects, not bare strings (cogame-garble)."""
    game = MANIFEST["game"]
    for key in ("player", "global"):
        entry = game["protocols"][key]
        assert isinstance(entry, dict) and set(entry) == {"type", "value"}
        assert entry["type"] == "uri" and entry["value"].startswith("https://")
    readme = game["docs"]["readme"]
    assert isinstance(readme, dict) and set(readme) == {"type", "value"}
    assert readme["type"] == "text"
    pages = game["docs"]["pages"]
    assert [p["id"] for p in pages] == ["rules.md", "replay.md"]
    for page in pages:
        assert set(page) == {"id", "title", "content"}
        assert set(page["content"]) == {"type", "value"}
        assert page["content"]["type"] == "text"


def test_the_inline_docs_are_the_repo_files_verbatim():
    """Inline text can drift from the file it was copied out of; this is the
    tripwire. Re-sync with:

        python3 - <<'PY'
        import json, pathlib
        ...  # see the assertion message
        PY
    """
    game = MANIFEST["game"]
    pages = {page["id"]: page for page in game["docs"]["pages"]}
    for key, path in DOC_SOURCES.items():
        value = (
            game["docs"]["readme"]["value"]
            if key == ("readme",)
            else pages[key[1]]["content"]["value"]
        )
        assert value == (REPO / path).read_text(), (
            f"game.docs {'/'.join(key)} has drifted from {path}; copy the file's "
            "text into coworld_manifest_template.json again"
        )


@pytest.mark.parametrize("path", ["README.md", "docs/RULES.md", "docs/REPLAY.md",
                                  "docs/PROTOCOL.md"])
def test_every_referenced_doc_exists_in_the_repo(path):
    """Every doc the manifest carries or points at is a real file here: the
    three inline pages are copies of DOC_SOURCES, and PROTOCOL.md is the uri
    `game.protocols` points at."""
    assert (REPO / path).is_file(), f"the manifest names {path}, which is missing"
    assert path in json.dumps(MANIFEST) or path in DOC_SOURCES.values()


# ------------------------------------------------------------ config schema
def test_config_schema_is_a_real_json_schema_with_bounded_arrays():
    schema = MANIFEST["game"]["config_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "tokens" in schema["required"], "the runner injects tokens"
    for name, prop in schema["properties"].items():
        if prop.get("type") == "array":
            assert "minItems" in prop and "maxItems" in prop, (
                f"config_schema.{name} is an array without minItems/maxItems "
                "(tandem 0.1.0)"
            )


def test_num_agents_is_pinned_to_four_in_the_schema():
    prop = MANIFEST["game"]["config_schema"]["properties"]["num_agents"]
    assert prop["minimum"] == prop["maximum"] == defaults.NUM_SEATS == 4


def test_the_rule_constants_are_pinned_so_a_variant_cannot_drift_them():
    props = MANIFEST["game"]["config_schema"]["properties"]
    for key, value in {
        "size": defaults.BOARD_SIZE,
        "spawn_cost": defaults.SPAWN_COST,
        "convert_cost": defaults.CONVERT_COST,
        "move_cost": defaults.MOVE_COST,
        "collect_rate": defaults.COLLECT_RATE,
        "regen_rate": defaults.REGEN_RATE,
        "max_cell_halite": defaults.MAX_CELL_HALITE,
    }.items():
        assert props[key]["minimum"] == props[key]["maximum"] == value, key


# ----------------------------------------------------------------- variants
def test_every_variant_declares_num_agents_inside_game_config():
    ids = [v["id"] for v in MANIFEST["variants"]]
    assert ids == ["standard", "sprint", "richfields"]
    for variant in MANIFEST["variants"]:
        assert variant["description"], f"variants[{variant['id']}].description is required"
        config = variant["game_config"]
        assert config["num_agents"] == 4, variant["id"]
        assert len(config["players"]) == 4, variant["id"]
        # CoworldVariant is additionalProperties:false and the platform reads
        # only game_config.num_agents (cogame-goofspiel-oshi-zumo 0.1.0).
        assert "num_agents" not in variant, (
            f"variants[{variant['id']}].num_agents at the TOP level is rejected"
        )
        assert set(variant) == {"id", "name", "description", "game_config"}


def test_no_game_config_carries_a_literal_tokens_array():
    """config_schema keeps REQUIRING tokens because the runner injects them,
    but an authored game_config must not carry one (cogame-knights-archers)."""
    blobs = [v["game_config"] for v in MANIFEST["variants"]]
    blobs.append(MANIFEST["certification"]["game_config"])
    for config in blobs:
        assert "tokens" not in config


def test_the_variants_differ_only_in_the_two_upstream_config_fields():
    base = MANIFEST["variants"][0]["game_config"]
    for variant in MANIFEST["variants"][1:]:
        config = variant["game_config"]
        changed = {k for k in base if base[k] != config.get(k)}
        assert changed <= {"episode_steps", "starting_halite", "directive_every"}, changed


def test_every_variant_game_config_validates_against_the_config_schema():
    from cogame_halite.config import GameConfig

    for variant in MANIFEST["variants"]:
        config = dict(variant["game_config"])
        config["tokens"] = [f"token-{i}" for i in range(4)]
        parsed = GameConfig.from_dict(config)
        assert parsed.num_seats == 4
        assert parsed.budget_guard_seconds < parsed.wall_clock_budget_seconds


# ------------------------------------------------------------ bundled players
def test_both_bundled_players_are_declared_with_the_platform_resource_floor():
    players = MANIFEST["player"]
    assert [p["id"] for p in players] == ["tidewalker", "corsair"]
    for entry in players:
        assert entry["type"] == "player"
        assert entry["name"] and entry["description"]
        assert entry["image"] == IMAGE_PLACEHOLDER
        assert entry["run"] == ["python", "-m", "players.halite_player"]
        resources = entry["resources"]
        assert resources["requests"] == {"cpu": "250m", "memory": "256Mi"}
        # The bundled player cpu LIMIT minimum is "1" (cogame-pistonball 0.1.1).
        assert resources["limits"]["cpu"] == "1"
        assert entry["env"]["PLAYER_SCRIPTED"] in defaults.BASELINES


def test_every_declared_player_occupies_a_certification_slot():
    """A player entry with no cert slot fails `players_missing` (raid 0.1.2)."""
    declared = {p["id"] for p in MANIFEST["player"]}
    seated = [p["player_id"] for p in MANIFEST["certification"]["players"]]
    assert declared <= set(seated), f"unseated: {declared - set(seated)}"
    assert seated == ["tidewalker", "corsair", "tidewalker", "corsair"]


# --------------------------------------------------------------- certification
def test_the_certification_fixture_is_the_four_seat_shape():
    cert = MANIFEST["certification"]
    config = cert["game_config"]
    assert config["num_agents"] == 4
    assert len(cert["players"]) == 4
    assert len(config["players"]) == 4
    assert config["episode_steps"] == 120
    assert config["seed"] == 42
    assert config["directive_every"] == 20
    assert config["player_connect_timeout_seconds"] == 60
    assert config["wall_clock_budget_seconds"] == 300


def test_the_fixture_fits_inside_the_local_certify_timeout():
    """`coworld certify` covers start + connect grace + the episode + the
    post-game linger. The directive SPACING floor is the dominant term for a
    scripted fixture, so it is zeroed here (cogame-commons-family 0.1.0)."""
    config = MANIFEST["certification"]["game_config"]
    assert config["directive_spacing_ms"] == 0, (
        "a 10 s spacing floor x 6 directive turns would alone exceed certify's default"
    )


# --------------------------------------------------------------- policies.json
def test_policies_json_is_two_llm_champions_and_two_scripted_fillers():
    rows = json.loads((REPO / "tools" / "ci" / "policies.json").read_text())
    assert [r["name"] for r in rows] == [
        "halite-tidereader", "halite-privateer", "halite-tidewalker", "halite-corsair"]
    champions = [r for r in rows if "PLAYER_PROMPT" in r["env"]]
    fillers = [r for r in rows if "PLAYER_SCRIPTED" in r["env"]]
    assert len(champions) == 2 and len(fillers) == 2
    # A scripted policy seated as a champion is a FAILURE state.
    for row in champions:
        assert row["env"]["USE_BEDROCK"] == "true"
        assert "PLAYER_SCRIPTED" not in row["env"]
    assert champions[0]["env"]["PLAYER_PROMPT"] != champions[1]["env"]["PLAYER_PROMPT"]
    # Champion #2 must be uploaded while daveey-1 is the active player.
    assert champions[1]["player"] == "ply_bac48eb1-662e-44f8-973d-f3e016dccf5d"
    assert "player" not in champions[0]
    for row in rows:
        assert row["run"] == ["python", "-m", "players.halite_player"]


# ----------------------------------------------------------------- workflows
@pytest.mark.parametrize("workflow", ["ci.yml", "coworld-release.yml", "coworld-submit.yml"])
def test_no_scaffold_placeholder_survives_in_a_workflow(workflow):
    text = (REPO / ".github" / "workflows" / workflow).read_text()
    for placeholder in ("<slug>", "<IMAGE>", "<SEATS>"):
        assert placeholder not in text, f"{workflow} still carries {placeholder}"


def test_the_release_workflow_exposes_the_four_inputs_and_the_artifact():
    text = (REPO / ".github" / "workflows" / "coworld-release.yml").read_text()
    for name in ("version:", "policies:", "put_secret:", "skip_certify:"):
        assert name in text
    assert "name: release-result" in text
    assert '"player"' in text or "player_id" in text, "the per-policy player field"
    assert "secret put" in text


def test_the_submit_workflow_exposes_its_three_inputs_and_the_artifact():
    text = (REPO / ".github" / "workflows" / "coworld-submit.yml").read_text()
    for name in ("player_id:", "policy:", "league_id:"):
        assert name in text
    assert "name: submit-result" in text


def test_the_hooks_are_committed_executable():
    import os
    import stat
    import subprocess

    for path in ("tools/ci/docker_smoke.sh", "tools/build_replay_viewer.sh"):
        mode = (REPO / path).stat().st_mode
        assert mode & stat.S_IXUSR, f"{path} must be executable (coworld build needs os.X_OK)"
        indexed = subprocess.run(
            ["git", "ls-files", "-s", path], cwd=REPO, capture_output=True, text=True
        ).stdout.split()
        if indexed:
            assert indexed[0] == "100755", f"{path} is mode {indexed[0]} in the git index"


def test_the_smoke_seat_count_agrees_with_the_fixture():
    text = (REPO / "tools" / "ci" / "docker_smoke.sh").read_text()
    assert 'seats_expected="${SMOKE_SEATS:-4}"' in text
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert 'SMOKE_SEATS: "4"' in ci


# ------------------------------------------------- the installed CLI's own check
def test_the_installed_coworld_cli_accepts_this_template():
    """0.1.42+ wants game.replay_viewer (not top level), no top-level version,
    no game.display_name, game.owner required and no runner-managed tokens in
    the fixture. Running the CLI's own loader is the only way to know."""
    bundle = pytest.importorskip(
        "coworld.bundle",
        reason="the `coworld` CLI is the dev dependency group: uv sync --frozen",
    )
    manifest = bundle._load_template_manifest(
        json.loads(MANIFEST_PATH.read_text()),
        GAME_VERSION,
        {IMAGE_PLACEHOLDER: f"coworld-halite:{GAME_VERSION}"},
    )
    document = manifest.model_dump(mode="json", exclude_none=True)
    bundle.validate_upload_manifest(document)


def test_the_game_version_is_plain_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", GAME_VERSION), (
        "version strings must be plain MAJOR.MINOR.PATCH; suffixes are rejected"
    )
