"""5. Baseline legality — the bounded-orders assertion.

Over 200 randomly generated boards x both baselines:

* at most **one action per owned asset**,
* only ids the seat owns,
* only enum values (and only the ones legal for that asset kind),
* at most **256 entries**,
* never a ``SPAWN`` the bank cannot pay,
* never a ``CONVERT`` onto a cell holding a shipyard,
* and the safety property: *never steps onto a cell adjacent to a strictly
  lighter enemy while carrying cargo, when a safe step exists*.

Plus determinism: the same state and directive compile the same orders twice.
"""

from __future__ import annotations

import random

import pytest

from conftest import make_config
from cogame_halite import defaults, micro
from cogame_halite.sim import HaliteSim

BOARDS = 200
SIZE = defaults.BOARD_SIZE
BASELINES = ("tidewalker", "corsair")


def random_board(rng: random.Random) -> HaliteSim:
    """A legal but arbitrary mid-game state: assets scattered, banks varied."""
    sim = HaliteSim(make_config(seed=rng.randrange(1 << 30)))
    sim.reset()
    sim.turn = rng.randrange(0, 400)
    sim.halite = [float(rng.choice([0, 0, 3, 42, 120, 310, 499])) for _ in range(SIZE * SIZE)]
    used: set[int] = set()

    def free_cell() -> int:
        while True:
            cell = rng.randrange(SIZE * SIZE)
            if cell not in used:
                used.add(cell)
                return cell

    sim.players = []
    for seat in range(4):
        yards = {}
        for i in range(rng.randrange(0, 3)):
            cell = free_cell()
            yards[f"{seat}y-{i}"] = cell
            sim.halite[cell] = 0.0
        ships = {
            f"{seat}s-{i}": [free_cell(), rng.choice([0, 5, 90, 250, 640, 1800])]
            for i in range(rng.randrange(0, 14))
        }
        sim.players.append([rng.choice([0, 120, 499, 500, 1500, 9000]), yards, ships])
    sim.eliminated = [None] * 4
    return sim


def all_boards():
    rng = random.Random(20260827)
    return [random_board(rng) for _ in range(BOARDS)]


BOARD_CACHE = all_boards()


@pytest.mark.parametrize("baseline", BASELINES)
def test_orders_are_bounded_and_legal_on_every_board(baseline):
    directive = micro.baseline_directive(baseline)
    checked = 0
    for sim in BOARD_CACHE:
        view = micro.BoardView.from_sim(sim)
        for seat in range(4):
            bank, yards, ships = sim.players[seat]
            orders = micro.compile_turn(view, seat, directive, baseline=baseline)
            checked += 1

            assert len(orders) <= defaults.MAX_ACTIONS_PER_TURN
            # one action per asset is structural (a dict), but the ids must all
            # be OWNED and each must be legal for its kind.
            spawns = 0
            for asset, action in orders.items():
                assert action in defaults.ALL_ACTIONS, (asset, action)
                assert asset in ships or asset in yards, f"seat {seat} does not own {asset}"
                if asset in ships:
                    assert action in defaults.SHIP_ACTIONS
                    if action == "CONVERT":
                        cell = int(ships[asset][0])
                        holders = {
                            int(p)
                            for other in range(4)
                            for p in sim.players[other][1].values()
                        }
                        assert cell not in holders, "CONVERT onto an occupied shipyard cell"
                        assert int(ships[asset][1]) + bank >= defaults.CONVERT_COST
                else:
                    assert action in defaults.SHIPYARD_ACTIONS
                    spawns += 1
            assert bank >= defaults.SPAWN_COST * spawns, (
                f"seat {seat} ordered {spawns} spawns on a bank of {bank}"
            )
    assert checked == BOARDS * 4


@pytest.mark.parametrize("baseline", BASELINES)
def test_never_steps_beside_a_strictly_lighter_enemy_when_a_safe_step_exists(baseline):
    """The safety property. A loaded ship that had a safe option must not have
    chosen a cell a strictly lighter enemy could ram it on."""
    directive = micro.baseline_directive(baseline)
    for sim in BOARD_CACHE:
        view = micro.BoardView.from_sim(sim)
        for seat in range(4):
            _bank, yards, ships = sim.players[seat]
            my_yards = {int(p) for p in yards.values()}
            threat: dict[int, int] = {}
            for other in range(4):
                if other == seat:
                    continue
                for pos, cargo in sim.players[other][2].values():
                    cells = [int(pos)] + [
                        micro.step_index(int(pos), d, SIZE) for d in micro.DIRECTION_ORDER
                    ]
                    for cell in cells:
                        if cell not in threat or int(cargo) < threat[cell]:
                            threat[cell] = int(cargo)

            def unsafe(cell: int, cargo: int) -> bool:
                """STRICTLY lighter — the design note's wording for this
                property. The micro's own predicate uses <=, because an
                equal-cargo collision kills both, so it is stricter still."""
                if cell in my_yards:
                    return False
                return cell in threat and threat[cell] < cargo

            orders = micro.compile_turn(view, seat, directive, baseline=baseline)
            for sid, (pos, cargo) in ships.items():
                pos, cargo = int(pos), int(cargo)
                if cargo <= 0:
                    continue
                action = orders.get(sid)
                if action in (None, "CONVERT"):
                    continue
                landed = micro.step_index(pos, action, SIZE)
                if not unsafe(landed, cargo):
                    continue
                options = [pos] + [
                    micro.step_index(pos, d, SIZE) for d in micro.DIRECTION_ORDER
                ]
                assert all(unsafe(cell, cargo) for cell in options), (
                    f"seat {seat} ship {sid} stepped into danger with a safe option available"
                )


@pytest.mark.parametrize("baseline", BASELINES)
def test_compile_turn_is_deterministic(baseline):
    directive = micro.baseline_directive(baseline)
    for sim in BOARD_CACHE[:40]:
        view = micro.BoardView.from_sim(sim)
        for seat in range(4):
            first = micro.compile_turn(view, seat, directive, baseline=baseline)
            second = micro.compile_turn(view, seat, directive, baseline=baseline)
            assert first == second


def test_compile_turn_is_pure():
    """It must not mutate the board it is handed."""
    sim = BOARD_CACHE[3]
    view = micro.BoardView.from_sim(sim)
    before = (list(view.halite), [[p[0], dict(p[1]), dict(p[2])] for p in view.players])
    micro.compile_turn(view, 0, micro.TIDEWALKER)
    after = (list(view.halite), [[p[0], dict(p[1]), dict(p[2])] for p in view.players])
    assert before == after


def test_corsair_and_tidewalker_play_visibly_different_games():
    """The fillers exist so the ladder's two baselines play different games and
    the collision rule is exercised in every episode, including the
    all-scripted CI smoke. Compared over seats that actually own a fleet: a
    seat with no ships compiles the empty map either way, which says nothing."""
    fleets = 0
    differences = 0
    for sim in BOARD_CACHE:
        view = micro.BoardView.from_sim(sim)
        for seat in range(4):
            if len(sim.players[seat][2]) < 3:
                continue
            fleets += 1
            a = micro.compile_turn(view, seat, micro.TIDEWALKER, baseline="tidewalker")
            b = micro.compile_turn(view, seat, micro.CORSAIR, baseline="corsair")
            if a != b:
                differences += 1
    assert fleets >= BOARDS, f"only {fleets} boards had a seat with a real fleet"
    # The two share the same executor and differ only in their constants
    # (mineFloor 100 vs 150, returnAt 500 vs 350, spawnUntil 300 vs 340) and in
    # corsair's always-on hunting, so a single random turn diverges on roughly a
    # quarter of fleets. The episode-level test below is the sharper one.
    assert differences >= fleets // 4, (
        f"the two baselines compiled the same orders on {fleets - differences} "
        f"of {fleets} fleets — they are not playing different games"
    )


def test_an_all_corsair_episode_diverges_from_an_all_tidewalker_one():
    """The sharp version of the test above: same seed, same board, 60 turns."""
    from conftest import make_sim, play_scripted

    a = make_sim()
    play_scripted(a, 60, baselines=("tidewalker",) * 4)
    b = make_sim()
    play_scripted(b, 60, baselines=("corsair",) * 4)
    assert a.state_hash() != b.state_hash()
    assert a.banks() != b.banks()
    # And the collision rule is exercised either way -- that is why corsair
    # exists at all.
    assert sum(s.collisions_won for s in b.stats) > 0


def test_the_shipyard_loss_guard_converts_when_the_seat_owns_no_yard():
    sim = BOARD_CACHE[0]
    sim.players[0] = [600, {}, {"0s-0": [50, 400], "0s-1": [90, 10]}]
    view = micro.BoardView.from_sim(sim)
    orders = micro.compile_turn(view, 0, micro.TIDEWALKER)
    assert orders.get("0s-0") == "CONVERT", "the heaviest funded ship converts"


def test_spawning_stops_at_the_directive_ceiling():
    sim = BOARD_CACHE[1]
    sim.turn = 350
    sim.players[0] = [9000, {"0y-0": 7}, {}]
    view = micro.BoardView.from_sim(sim)
    directive = micro.Directive(spawnUntil=300)
    assert "0y-0" not in micro.compile_turn(view, 0, directive)
    assert micro.compile_turn(view, 0, micro.Directive(spawnUntil=400))["0y-0"] == "SPAWN"


def test_baseline_lookup_falls_back_to_tidewalker():
    assert micro.baseline_directive("nonsense") is micro.TIDEWALKER
    assert micro.baseline_directive("corsair") is micro.CORSAIR
