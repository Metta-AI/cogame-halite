"""3. Per-rule sim tests — one per numbered rule of the design note's
§"Turn resolution — the exact order".

The rules are the vendored ``Board.next()``'s, not ours; these assertions pin
the *behaviour* so a future re-vendor that changes one is caught by name rather
than as a wall of fidelity-gate diff.
"""

from __future__ import annotations

import pytest

from conftest import make_config, make_sim
from cogame_halite import defaults
from cogame_halite.sim import HaliteGuardError, HaliteSim, _translate

SIZE = defaults.BOARD_SIZE


def sim_with(state, **overrides) -> HaliteSim:
    """A sim forced into a hand-written state (turn 0)."""
    sim = HaliteSim(make_config(**overrides))
    sim.reset()
    sim.halite = [0.0] * (SIZE * SIZE)
    sim.players = [[0, {}, {}] for _ in range(4)]
    sim.eliminated = [None] * 4
    for seat, (bank, yards, ships) in enumerate(state):
        sim.players[seat] = [bank, dict(yards), {k: list(v) for k, v in ships.items()}]
    return sim


def idx(x: int, y: int) -> int:
    return (SIZE - y - 1) * SIZE + x


# --------------------------------------------------------------------- 1, 2a
def test_spawn_is_processed_before_convert_and_stops_at_the_bank():
    sim = sim_with([
        (1200, {"y0": idx(5, 5), "y1": idx(6, 5), "y2": idx(7, 5)}, {}),
        (0, {}, {}), (0, {}, {}), (0, {}, {}),
    ])
    sim.step([{"y0": "SPAWN", "y1": "SPAWN", "y2": "SPAWN"}, {}, {}, {}])
    # 1200 covers two spawns, in shipyard-insertion order; the third is unfunded.
    assert sim.players[0][0] == 200
    assert len(sim.players[0][2]) == 2
    positions = [v[0] for v in sim.players[0][2].values()]
    assert positions == [idx(5, 5), idx(6, 5)]


def test_a_spawned_ship_has_zero_cargo_and_a_fresh_uid():
    sim = sim_with([(600, {"y0": idx(5, 5)}, {}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.step([{"y0": "SPAWN"}, {}, {}, {}])
    (uid, (pos, cargo)), = sim.players[0][2].items()
    assert cargo == 0 and pos == idx(5, 5)
    assert uid == "1-1", "uids are minted f'{turn}-{n}', n counting from 1 across ALL seats"


def test_uid_minting_counts_across_seats_within_one_resolution():
    sim = sim_with([
        (600, {"a": idx(2, 2)}, {}),
        (600, {"b": idx(4, 4)}, {}),
        (600, {"c": idx(6, 6)}, {}),
        (0, {}, {}),
    ])
    sim.step([{"a": "SPAWN"}, {"b": "SPAWN"}, {"c": "SPAWN"}, {}])
    minted = [next(iter(sim.players[s][2])) for s in range(3)]
    assert minted == ["1-1", "1-2", "1-3"]


def test_the_four_opening_ships_are_0_1_through_0_4():
    sim = make_sim()
    assert [next(iter(p[2])) for p in sim.players] == ["0-1", "0-2", "0-3", "0-4"]


# ----------------------------------------------------------------------- 2b
def test_convert_funds_from_cargo_and_zeroes_the_cell():
    # A second ship keeps the seat alive: after the convert it would otherwise
    # be shipless with a bank under the spawn cost, which is elimination.
    sim = sim_with([
        (100, {}, {"s": [idx(5, 5), 450], "keep": [idx(15, 15), 0]}),
        (0, {}, {}), (0, {}, {}), (0, {}, {}),
    ])
    sim.halite[idx(5, 5)] = 300.0
    sim.step([{"s": "CONVERT"}, {}, {}, {}])
    # delta = 450 - 500 = -50 -> bank += min(delta, 0)
    assert sim.players[0][0] == 50
    assert len(sim.players[0][1]) == 1 and list(sim.players[0][2]) == ["keep"]
    assert sim.halite[idx(5, 5)] == 0.0, "a cell that becomes a shipyard loses its halite"


def test_leftover_convert_halite_is_held_aside_until_after_every_convert():
    """Upstream's explicit guard against chaining one convert's change into
    another: a rich ship's excess cannot fund a second convert this turn."""
    sim = sim_with([
        (0, {}, {"rich": [idx(5, 5), 1500], "poor": [idx(9, 9), 0]}),
        (0, {}, {}), (0, {}, {}), (0, {}, {}),
    ])
    sim.step([{"rich": "CONVERT", "poor": "CONVERT"}, {}, {}, {}])
    assert len(sim.players[0][1]) == 1, "the poor ship must NOT be funded by the rich one"
    assert sim.players[0][0] == 1000, "the excess lands in the bank only after the block"
    assert "poor" in sim.players[0][2]


def test_convert_is_refused_on_a_cell_that_already_holds_a_shipyard():
    sim = sim_with([(5000, {"y": idx(5, 5)}, {"s": [idx(5, 5), 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.step([{"s": "CONVERT"}, {}, {}, {}])
    assert len(sim.players[0][1]) == 1
    assert "s" in sim.players[0][2], "the ship survives; the convert was simply not applied"


# ------------------------------------------------------------------- 2c, 3, 4
@pytest.mark.parametrize(
    "direction,delta",
    [("NORTH", (0, 1)), ("SOUTH", (0, -1)), ("EAST", (1, 0)), ("WEST", (-1, 0))],
)
def test_moves_and_torus_wrap_in_all_four_directions(direction, delta):
    for start in ((0, 0), (SIZE - 1, SIZE - 1), (10, 0), (0, 10)):
        sim = sim_with([(0, {}, {"s": [idx(*start), 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
        sim.step([{"s": direction}, {}, {}, {}])
        want = idx((start[0] + delta[0]) % SIZE, (start[1] + delta[1]) % SIZE)
        assert sim.players[0][2]["s"][0] == want, f"{direction} from {start}"


def test_move_cost_is_zero_so_cargo_is_unchanged():
    sim = sim_with([(0, {}, {"s": [idx(5, 5), 777]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.step([{"s": "NORTH"}, {}, {}, {}])
    assert sim.players[0][2]["s"][1] == 777


def test_head_on_swaps_do_not_collide():
    """Collisions resolve AFTER all movement, so A->B and B->A pass through."""
    a, b = idx(5, 5), idx(6, 5)
    sim = sim_with([
        (0, {}, {"a": [a, 10]}),
        (0, {}, {"b": [b, 20]}),
        (0, {}, {}), (0, {}, {}),
    ])
    sim.step([{"a": "EAST"}, {"b": "WEST"}, {}, {}])
    assert sim.players[0][2]["a"][0] == b
    assert sim.players[1][2]["b"][0] == a


def test_the_lighter_ship_survives_and_absorbs_every_lost_cargo():
    cell = idx(5, 5)
    sim = sim_with([
        (0, {}, {"light": [idx(4, 5), 10]}),
        (0, {}, {"heavy": [idx(6, 5), 500]}),
        (0, {}, {}), (0, {}, {}),
    ])
    result = sim.step([{"light": "EAST"}, {"heavy": "WEST"}, {}, {}])
    assert sim.players[0][2]["light"] == [cell, 510]
    assert not sim.players[1][2]
    kinds = [e for e in result.events if e["k"] == "collide"]
    assert kinds and kinds[0]["survivor"]["seat"] == 0 and kinds[0]["stolen"] == 500


def test_equal_cargo_destroys_every_ship_on_the_cell():
    sim = sim_with([
        (0, {}, {"a": [idx(4, 5), 300]}),
        (0, {}, {"b": [idx(6, 5), 300]}),
        (0, {}, {}), (0, {}, {}),
    ])
    result = sim.step([{"a": "EAST"}, {"b": "WEST"}, {}, {}])
    assert not sim.players[0][2] and not sim.players[1][2]
    collide = next(e for e in result.events if e["k"] == "collide")
    assert collide["survivor"] is None and collide["stolen"] == 0


def test_three_way_pile_up_leaves_the_strictly_lightest():
    sim = sim_with([
        (0, {}, {"a": [idx(4, 5), 100]}),
        (0, {}, {"b": [idx(6, 5), 50]}),
        (0, {}, {"c": [idx(5, 6), 900]}),
        (0, {}, {}),
    ])
    sim.step([{"a": "EAST"}, {"b": "WEST"}, {"c": "SOUTH"}, {}])
    assert not sim.players[0][2]
    assert sim.players[1][2]["b"][1] == 50 + 100 + 900
    assert not sim.players[2][2]


def test_friendly_fire_uses_exactly_the_same_rule():
    sim = sim_with([
        (0, {}, {"a": [idx(4, 5), 100], "b": [idx(6, 5), 40]}),
        (0, {}, {}), (0, {}, {}), (0, {}, {}),
    ])
    sim.step([{"a": "EAST", "b": "WEST"}, {}, {}, {}])
    assert "a" not in sim.players[0][2]
    assert sim.players[0][2]["b"][1] == 140


def test_an_enemy_ship_on_a_shipyard_destroys_both():
    cell = idx(5, 5)
    sim = sim_with([
        (0, {"y": cell}, {}),
        (0, {}, {"raider": [idx(4, 5), 0]}),
        (0, {}, {}), (0, {}, {}),
    ])
    result = sim.step([{}, {"raider": "EAST"}, {}, {}])
    assert not sim.players[0][1] and not sim.players[1][2]
    raze = next(e for e in result.events if e["k"] == "yardraze")
    assert raze["yardSeat"] == 0 and raze["shipSeat"] == 1 and raze["pos"] == cell


# ------------------------------------------------------------------------- 5
def test_deposit_happens_after_collisions():
    """A loaded ship rammed on the doorstep of its own shipyard loses
    everything: the survivor banks, the loser does not."""
    yard = idx(5, 5)
    sim = sim_with([
        (0, {"y": yard}, {"mine": [idx(4, 5), 900]}),
        (0, {}, {"light": [idx(6, 5), 5]}),
        (0, {}, {}), (0, {}, {}),
    ])
    sim.step([{"mine": "EAST"}, {"light": "WEST"}, {}, {}])
    assert sim.players[0][0] == 0, "the heavy ship never reached the bank"
    assert not sim.players[0][2]
    # The lighter enemy is now standing on someone else's yard, so both died.
    assert not sim.players[1][2] and not sim.players[0][1]


def test_deposit_banks_the_whole_hold_and_zeroes_the_cargo():
    yard = idx(5, 5)
    sim = sim_with([(0, {"y": yard}, {"s": [idx(4, 5), 640]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    result = sim.step([{"s": "EAST"}, {}, {}, {}])
    assert sim.players[0][0] == 640
    assert sim.players[0][2]["s"] == [yard, 0]
    deposit = next(e for e in result.events if e["k"] == "deposit")
    assert deposit["amount"] == 640 and deposit["seat"] == 0


# ------------------------------------------------------------------------- 6
def test_mining_takes_a_truncated_quarter_of_the_cell():
    sim = sim_with([(0, {}, {"s": [idx(5, 5), 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[idx(5, 5)] = 199.0
    sim.step([{}, {}, {}, {}])
    assert sim.players[0][2]["s"][1] == 49, "int(199 * 0.25) == 49"
    assert sim.halite[idx(5, 5)] == 150.0


def test_a_ship_that_moved_does_not_mine():
    sim = sim_with([(0, {}, {"s": [idx(5, 5), 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[idx(5, 5)] = 400.0
    sim.halite[idx(6, 5)] = 0.0
    sim.step([{"s": "EAST"}, {}, {}, {}])
    assert sim.players[0][2]["s"][1] == 0


def test_a_ship_on_a_shipyard_does_not_mine():
    cell = idx(5, 5)
    sim = sim_with([(0, {"y": cell}, {"s": [cell, 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[cell] = 0.0
    sim.step([{}, {}, {}, {}])
    assert sim.players[0][2]["s"][1] == 0


def test_a_zero_delta_is_not_a_mine():
    sim = sim_with([(0, {}, {"s": [idx(5, 5), 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[idx(5, 5)] = 3.0  # int(3 * 0.25) == 0
    result = sim.step([{}, {}, {}, {}])
    assert sim.players[0][2]["s"][1] == 0
    assert not [e for e in result.events if e["k"] == "mine"]


def test_a_ship_spawned_this_turn_never_mines_on_its_birth_turn():
    cell = idx(5, 5)
    sim = sim_with([(600, {"y": cell}, {}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[cell] = 0.0
    sim.step([{"y": "SPAWN"}, {}, {}, {}])
    assert list(sim.players[0][2].values())[0][1] == 0


# ------------------------------------------------------------------------- 7
def test_regeneration_skips_cells_under_a_ship():
    under, free = idx(5, 5), idx(9, 9)
    sim = sim_with([(0, {}, {"s": [under, 0]}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[under] = 100.0
    sim.halite[free] = 100.0
    sim.step([{}, {}, {}, {}])
    assert sim.halite[under] == 75.0, "mined, never regrown"
    assert sim.halite[free] == 102.0


def test_regeneration_rounds_to_three_decimals_and_caps_at_500():
    a, b = idx(3, 3), idx(4, 4)
    sim = sim_with([(0, {}, {}), (0, {}, {}), (0, {}, {}), (0, {}, {})])
    sim.halite[a] = 1.0 / 3.0
    sim.halite[b] = 499.0
    sim.step([{}, {}, {}, {}])
    assert sim.halite[a] == round((1.0 / 3.0) * 1.02, 3)
    assert sim.halite[b] == defaults.MAX_CELL_HALITE


# ---------------------------------------------------------------------- 8, 9
def test_the_turn_counter_advances_by_one():
    sim = make_sim()
    assert sim.turn == 0
    sim.step([{}, {}, {}, {}])
    assert sim.turn == 1


def test_elimination_and_the_negative_score():
    from cogame_halite.results import score_of

    sim = sim_with([
        (0, {}, {"a": [idx(4, 5), 300]}),
        (0, {}, {"b": [idx(6, 5), 300]}),
        (5000, {"c": idx(1, 1)}, {"c1": [idx(1, 2), 0]}),
        (5000, {"d": idx(2, 2)}, {"d1": [idx(2, 3), 0]}),
    ], episode_steps=400)
    result = sim.step([{"a": "EAST"}, {"b": "WEST"}, {}, {}])
    assert sorted(result.eliminated_this_turn) == [0, 1]
    assert sim.eliminated[0] == sim.eliminated[1] == 1
    assert score_of(0, 1, 400) == 1 - 400 - 1 == -400


def test_a_seat_with_a_funded_shipyard_is_not_eliminated_without_ships():
    sim = sim_with([
        (500, {"y": idx(5, 5)}, {}),
        (5000, {}, {"b": [idx(1, 1), 0]}),
        (5000, {}, {"c": [idx(2, 2), 0]}),
        (5000, {}, {"d": [idx(3, 3), 0]}),
    ])
    sim.step([{}, {}, {}, {}])
    assert sim.eliminated[0] is None


def test_an_unfunded_shipyard_does_not_save_a_shipless_seat():
    sim = sim_with([
        (499, {"y": idx(5, 5)}, {}),
        (5000, {}, {"b": [idx(1, 1), 0]}),
        (5000, {}, {"c": [idx(2, 2), 0]}),
        (5000, {}, {"d": [idx(3, 3), 0]}),
    ])
    sim.step([{}, {}, {}, {}])
    assert sim.eliminated[0] == 1


def test_an_eliminated_seat_can_never_act_again():
    sim = sim_with([
        (0, {}, {}),
        (5000, {"y": idx(9, 9)}, {"b": [idx(1, 1), 0]}),
        (5000, {"z": idx(8, 8)}, {"c": [idx(2, 2), 0]}),
        (5000, {"w": idx(7, 7)}, {"d": [idx(3, 3), 0]}),
    ])
    sim.step([{}, {}, {}, {}])
    assert sim.eliminated[0] == 1
    assert sim.players[0] == [0, {}, {}]


def test_an_eliminated_seat_keeps_its_shipyard_and_it_stays_a_hazard():
    """Upstream (``halite.py`` lines 195-202) clears ``obs.players[index]``
    only for a status that is neither ACTIVE nor DONE. Elimination makes a seat
    **DONE**, so its unfunded shipyard stays on the board — and an enemy ship
    that walks onto it is destroyed with it. Clearing the assets here would be
    fixing an upstream quirk."""
    sim = sim_with([
        (499, {"y0": idx(5, 5)}, {}),
        (5000, {"y1": idx(9, 9)}, {"b": [idx(5, 6), 0]}),
        (5000, {"y2": idx(8, 8)}, {"c": [idx(2, 2), 0]}),
        (5000, {"y3": idx(7, 7)}, {"d": [idx(3, 3), 0]}),
    ])
    sim.step([{}, {}, {}, {}])
    assert sim.eliminated[0] == 1
    assert sim.players[0] == [499, {"y0": idx(5, 5)}, {}], (
        "the eliminated seat's shipyard must survive: upstream keeps it"
    )

    # Seat 1 steps onto the abandoned yard: both the yard and the ship die.
    result = sim.step([{}, {"b": "SOUTH"}, {}, {}])
    assert sim.players[0][1] == {}
    assert sim.players[1][2] == {}
    assert any(event["k"] == "yardraze" for event in result.events)


# ----------------------------------------------------------------------- 10
def test_last_fleet_ends_the_episode():
    sim = sim_with([
        (0, {}, {}), (0, {}, {}), (0, {}, {}),
        (5000, {"y": idx(9, 9)}, {"d": [idx(3, 3), 0]}),
    ])
    result = sim.step([{}, {}, {}, {}])
    assert result.last_fleet and sim.last_fleet


# ------------------------------------------------------------------- guards
def test_an_unknown_action_reaching_the_sim_is_a_guard_error():
    sim = make_sim()
    with pytest.raises(HaliteGuardError):
        sim.step([{"0-1": "TELEPORT"}, {}, {}, {}])


def test_an_oversized_order_map_reaching_the_sim_is_a_guard_error():
    sim = make_sim()
    orders = {f"x-{i}": "NORTH" for i in range(defaults.MAX_ACTIONS_PER_TURN + 1)}
    with pytest.raises(HaliteGuardError):
        sim.step([orders, {}, {}, {}])


def test_a_wrong_seat_count_is_a_guard_error():
    sim = make_sim()
    with pytest.raises(HaliteGuardError):
        sim.step([{}, {}, {}])


# ------------------------------------------------------------------ geometry
def test_index_zero_is_the_top_left_cell():
    assert idx(0, SIZE - 1) == 0
    assert idx(SIZE - 1, 0) == SIZE * SIZE - 1


def test_north_is_index_minus_size():
    assert _translate(idx(5, 5), "NORTH", SIZE) == idx(5, 5) - SIZE
    assert _translate(idx(5, 5), "SOUTH", SIZE) == idx(5, 5) + SIZE
    assert _translate(idx(5, 5), "EAST", SIZE) == idx(5, 5) + 1
    assert _translate(idx(5, 5), "WEST", SIZE) == idx(5, 5) - 1


def test_the_ascii_board_is_the_vendored_str():
    sim = make_sim()
    board = sim.ascii_board()
    lines = board.splitlines()
    assert len(lines) == SIZE
    # Upstream writes "|<ship><digit><yard>" per cell, then a closing "|".
    assert all(len(line) == SIZE * 4 + 1 for line in lines)
    assert board.count("a") + board.count("b") + board.count("c") + board.count("d") == 4
