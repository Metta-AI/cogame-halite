"""9. Results — the closed key set in three places, the formula, the ladder."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cogame_halite import defaults, results as results_mod
from cogame_halite.results import RESULTS_KEYS, build, placement_and_ranking, score_of

REPO = Path(__file__).resolve().parents[1]


def sample(**overrides) -> dict:
    data = dict(
        names=["daveey", "daveey-1", "halite-tidewalker", "halite-corsair"],
        aliases=list(defaults.ALIASES),
        banked=[4000, 3000, 2000, 1000],
        ships=[5, 4, 3, 2],
        yards=[2, 2, 1, 1],
        mined=[9000, 8000, 7000, 6000],
        stolen=[100, 200, 300, 400],
        collisions_won=[3, 2, 1, 0],
        collisions_lost=[0, 1, 2, 3],
        eliminated_turn=[None, None, None, None],
        llm_turns=[20, 20, 0, 0],
        fallbacks=[{} for _ in range(4)],
        dead_seats=[False] * 4,
        reason="complete",
        end_rule="full_time",
        final_turn=399,
        seed=8675309,
        episode_steps=400,
    )
    data.update(overrides)
    return build(**data)


# ------------------------------------------------- the closed key set, x3
def test_the_document_has_exactly_the_declared_keys():
    doc = sample()
    assert tuple(doc) == RESULTS_KEYS


def test_the_manifest_results_schema_is_the_same_closed_set():
    manifest = json.loads((REPO / "coworld_manifest_template.json").read_text())
    schema = manifest["game"]["results_schema"]
    assert schema["additionalProperties"] is False, "the results schema must be CLOSED"
    assert tuple(schema["properties"]) == RESULTS_KEYS, (
        "results.py and the manifest results_schema disagree"
    )
    assert sorted(schema["required"]) == sorted(RESULTS_KEYS)


def test_the_docker_smoke_expected_key_set_is_the_same():
    """Three places, one list. `docker_smoke.sh` carries the third copy
    literally (it runs on a host that may not have the package importable) and
    ci.yml's own assertion imports RESULTS_KEYS from the code, so neither can
    drift alone."""
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "from cogame_halite.results import RESULTS_KEYS" in workflow
    assert "tuple(doc) != RESULTS_KEYS" in workflow
    smoke = (REPO / "tools" / "ci" / "docker_smoke.sh").read_text()
    literal = re.search(r"RESULTS_KEYS = \(\n(.*?)\n\)\n", smoke, flags=re.S)
    assert literal, "docker_smoke.sh must carry the closed key set literally"
    keys = tuple(re.findall(r'"([a-z_]+)"', literal.group(1)))
    assert keys == RESULTS_KEYS, (
        "tools/ci/docker_smoke.sh and results.py disagree:\n"
        f"  smoke {keys}\n  code  {RESULTS_KEYS}"
    )
    assert "tuple(results) != RESULTS_KEYS" in smoke, "and it must ASSERT it"


def test_the_docker_smoke_requires_a_complete_episode():
    """The design note's §Tests 14: the smoke script itself asserts
    `reason == "complete"`. Leaving that to a ci.yml step means the local
    invocation AGENTS.md documents checks neither."""
    smoke = (REPO / "tools" / "ci" / "docker_smoke.sh").read_text()
    assert 'reason = results["reason"]' in smoke
    assert 'if reason != "complete":' in smoke


def test_the_reason_enum_is_closed_in_both_places():
    manifest = json.loads((REPO / "coworld_manifest_template.json").read_text())
    schema = manifest["game"]["results_schema"]["properties"]
    assert schema["reason"]["enum"] == list(defaults.REASONS)
    assert schema["end_rule"]["enum"] == list(defaults.END_RULES)
    assert sorted(schema["fallbacks"]["items"]["properties"]) == sorted(defaults.FALLBACK_CAUSES)


def test_an_unknown_reason_or_end_rule_is_refused():
    with pytest.raises(ValueError):
        sample(reason="fine")
    with pytest.raises(ValueError):
        sample(end_rule="vibes")


# ------------------------------------------------------- formula and sign
def test_a_surviving_seat_scores_its_bank():
    assert score_of(4200, None, 400) == 4200


def test_an_eliminated_seat_scores_negative_and_later_is_better():
    assert score_of(9999, 100, 400) == 100 - 400 - 1 == -301
    assert score_of(0, 300, 400) == -101
    assert score_of(0, 300, 400) > score_of(0, 100, 400)


def test_a_survivor_always_outranks_an_eliminated_seat():
    doc = sample(banked=[0, 0, 0, 0], eliminated_turn=[None, 399, 200, 100])
    assert doc["scores"] == [0, -2, -201, -301]
    assert doc["ranking"] == [0, 1, 2, 3]
    assert doc["placement"] == [1, 2, 3, 4]
    assert doc["winner"] == 0


# --------------------------------------------------------- the tie-break ladder
def test_rule_two_breaks_a_score_tie_on_assets():
    placement, ranking = placement_and_ranking([10, 10, 5, 1], [1, 4, 9, 9], [0, 0, 0, 0])
    assert ranking == [1, 0, 2, 3]
    assert placement == [2, 1, 3, 4]


def test_rule_three_breaks_a_score_and_asset_tie_on_mined():
    placement, ranking = placement_and_ranking([10, 10, 10, 1], [2, 2, 2, 0], [5, 9, 7, 0])
    assert ranking == [1, 2, 0, 3]
    assert placement == [3, 1, 2, 4]


def test_rule_four_gives_a_strict_ranking_for_a_three_way_tie():
    placement, ranking = placement_and_ranking([7, 7, 7, 1], [2, 2, 2, 0], [3, 3, 3, 0])
    assert ranking == [0, 1, 2, 3], "seat index always terminates the ladder"
    assert placement == [1, 1, 1, 4], "equal seats share the higher placement"


def test_a_shared_first_place_has_no_winner():
    doc = sample(banked=[10, 10, 1, 1], ships=[1, 1, 1, 1], yards=[1, 1, 1, 1],
                 mined=[5, 5, 5, 5])
    assert doc["placement"] == [1, 1, 3, 3]
    assert doc["win"] == [True, True, False, False]
    assert doc["winner"] is None


def test_win_is_placement_one():
    doc = sample()
    assert doc["win"] == [p == 1 for p in doc["placement"]]
    assert doc["winner"] == 0


def test_a_four_way_dead_heat_ranks_by_seat():
    doc = sample(banked=[0] * 4, ships=[0] * 4, yards=[0] * 4, mined=[0] * 4)
    assert doc["placement"] == [1, 1, 1, 1]
    assert doc["ranking"] == [0, 1, 2, 3]
    assert doc["winner"] is None


# ------------------------------------------------------------- deadline path
def test_a_deadline_episode_is_still_scored_and_ranked():
    doc = sample(reason="deadline", end_rule="wall_clock", final_turn=137)
    assert doc["scores"] == doc["banked"], "never zeroed"
    assert sorted(doc["ranking"]) == [0, 1, 2, 3]
    assert doc["final_turn"] == 137


def test_fallbacks_are_always_the_full_cause_partition():
    doc = sample(fallbacks=[{"timeout": 3}, {}, {"host_error": 1}, {}])
    for counts in doc["fallbacks"]:
        assert sorted(counts) == sorted(defaults.FALLBACK_CAUSES)
    assert doc["fallbacks"][0]["timeout"] == 3
    assert doc["fallbacks"][0]["malformed"] == 0


def test_stop_detail_is_rune_truncated():
    doc = sample(stop_detail="\U0001F6A2" * 900)
    assert len(doc["stop_detail"]) == defaults.MAX_STOP_DETAIL_RUNES
    assert doc["stop_detail"].encode("utf-8").decode("utf-8") == doc["stop_detail"]


def test_the_document_round_trips_through_strict_json():
    raw = json.dumps(sample(stop_detail="\U0001F6A2 fault")).encode("utf-8")
    assert json.loads(raw.decode("utf-8"))["stop_detail"].startswith("\U0001F6A2")
