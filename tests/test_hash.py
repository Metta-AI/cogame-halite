"""4. ``state_hash`` — stable across processes, sensitive to any single field."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import make_config, make_sim, play_scripted
from cogame_halite.sim import state_hash_of

REPO = Path(__file__).resolve().parents[1]


def test_hash_is_sixteen_hex_digits():
    digest = make_sim().state_hash()
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_is_stable_across_processes():
    """A hash that depends on PYTHONHASHSEED cannot pin a replay."""
    sim = make_sim()
    play_scripted(sim, 12)
    here = sim.state_hash()
    script = (
        "import sys;"
        f"sys.path[:0]=[{str(REPO / 'server')!r}, {str(REPO)!r}, {str(REPO / 'tests')!r}];"
        "from conftest import make_sim, play_scripted;"
        "sim = make_sim(); play_scripted(sim, 12); print(sim.state_hash())"
    )
    for seed_env in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed_env},
            cwd=str(REPO),
        )
        assert out.stdout.strip() == here, f"PYTHONHASHSEED={seed_env} changed the hash"


def _base():
    return dict(
        turn=7,
        halite=[float(i) for i in range(9)],
        players=[[100, {"y1": 3}, {"s1": [4, 20]}], [200, {}, {"s2": [5, 0]}]],
        eliminated=[None, None],
    )


def test_every_single_field_perturbation_changes_the_hash():
    base = _base()
    reference = state_hash_of(**base)

    def perturbed(**changes):
        payload = json.loads(json.dumps(base))
        payload.update(changes)
        return state_hash_of(**payload)

    assert perturbed(turn=8) != reference
    halite = list(base["halite"]); halite[3] += 0.001
    assert perturbed(halite=halite) != reference
    banks = json.loads(json.dumps(base["players"])); banks[0][0] = 101
    assert perturbed(players=banks) != reference
    yards = json.loads(json.dumps(base["players"])); yards[0][1]["y1"] = 4
    assert perturbed(players=yards) != reference
    ships = json.loads(json.dumps(base["players"])); ships[0][2]["s1"][1] = 21
    assert perturbed(players=ships) != reference
    moved = json.loads(json.dumps(base["players"])); moved[0][2]["s1"][0] = 5
    assert perturbed(players=moved) != reference
    assert perturbed(eliminated=[3, None]) != reference


def test_dict_ordering_does_not_change_the_hash():
    """The hash sorts assets, so it pins STATE, not serialisation order."""
    a = state_hash_of(1, [0.0], [[0, {"b": 2, "a": 1}, {}]], [None])
    b = state_hash_of(1, [0.0], [[0, {"a": 1, "b": 2}, {}]], [None])
    assert a == b


def test_hash_changes_on_every_turn_of_a_real_episode():
    sim = make_sim()
    seen = {sim.state_hash()}
    play_scripted(sim, 1)
    for _ in range(20):
        play_scripted(sim, 1)
        seen.add(sim.state_hash())
    assert len(seen) >= 20, "a hash that barely moves cannot detect a divergence"
