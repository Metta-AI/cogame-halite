"""The order streams both sides of the fidelity gate replay.

Generated from OUR sim's state and recorded; the upstream reference replays the
recorded stream. There are two of them.

:func:`order_stream_step` — the random-but-legal stream. Two properties make it
a usable gate rather than a lottery:

* **Nothing is elided.** Ships move, mine, convert and collide with each other
  freely, so the ram rule, the leftover-convert hold-aside, deposits,
  regeneration and uid minting are all exercised at every turn.
* **No seat can be eliminated**, so the episode reaches the full 399 turns and
  the gate's tick-count floor is real. Two rules buy that: every seat converts
  its opening ship (so it always owns a shipyard) and no ship is ever ordered
  onto **another seat's** shipyard, which is the only way a shipyard can be
  destroyed. Banks are kept above the spawn cost.

:func:`elimination_stream_step` — the same stream with one seat deliberately
bankrupted, because a stream in which nothing is ever eliminated cannot see the
elimination transcription at all. It drives seat :data:`VICTIM_SEAT` to
**0 ships, a bank under the spawn cost and a shipyard still standing** — the
exact state where upstream keeps the yard (``halite.py`` clears assets only for
a non-DONE status) — and then walks a :data:`RAIDER_SEAT` ship onto that
abandoned yard, so the raze hazard the yard represents is compared too.
"""

from __future__ import annotations

import random

from cogame_halite import micro

MOVES = ("NORTH", "SOUTH", "EAST", "WEST")

#: Never spend the bank below this: a seat with no ships and a bank under the
#: spawn cost is eliminated at the end of the turn.
BANK_FLOOR = 2000

SPAWN_COST = 500

#: The seat :func:`elimination_stream_step` bankrupts, and the seat that razes
#: its abandoned shipyard afterwards.
VICTIM_SEAT = 0
RAIDER_SEAT = 1


def order_stream_step(sim, rng: random.Random) -> list[dict[str, str]]:
    """One turn of orders for every seat."""
    size = sim.size
    foreign_yards: dict[int, set[int]] = {}
    all_yards: set[int] = set()
    for seat in range(sim.num_seats):
        cells = {int(p) for p in sim.players[seat][1].values()}
        foreign_yards[seat] = cells
        all_yards |= cells

    orders: list[dict[str, str]] = []
    for seat in range(sim.num_seats):
        bank, yards, ships = sim.players[seat]
        mine_yards = foreign_yards[seat]
        enemy_yards = all_yards - mine_yards
        mapping: dict[str, str] = {}
        ship_ids = list(ships)

        if not yards and ship_ids:
            # Opening move: convert, so the seat always owns a shipyard.
            mapping[ship_ids[0]] = "CONVERT"
            ship_ids = ship_ids[1:]

        for sid in ship_ids:
            pos = int(ships[sid][0])
            roll = rng.random()
            if roll < 0.55:
                direction = rng.choice(MOVES)
                # Never step onto another seat's shipyard: that would destroy
                # it, and a seat that loses every shipyard while it has no
                # ships is eliminated, which ends the upstream episode early
                # and would silently shrink the gate.
                if micro.step_index(pos, direction, size) not in enemy_yards:
                    mapping[sid] = direction
            elif roll < 0.60 and len(yards) < 3 and bank >= BANK_FLOOR and pos not in all_yards:
                mapping[sid] = "CONVERT"
            # else: no action == mine

        spawned = 0
        for yid in yards:
            if bank - 500 * spawned < BANK_FLOOR:
                break
            if len(ships) + spawned >= 18:
                break
            if rng.random() < 0.35:
                mapping[yid] = "SPAWN"
                spawned += 1
        orders.append(mapping)
    return orders


def _step_toward(pos: int, target: int, size: int) -> str | None:
    """The first direction in ``micro.DIRECTION_ORDER`` that closes the gap."""
    here = micro.dist(pos, target, size)
    for direction in micro.DIRECTION_ORDER:
        if micro.dist(micro.step_index(pos, direction, size), target, size) < here:
            return direction
    return None


def _victim_orders(sim, seat: int) -> dict[str, str]:
    """Bankrupt ``seat`` while it keeps a shipyard.

    The elimination rule is "no ships **and** (no shipyard **or** bank below
    the spawn cost)", so reaching it with a yard standing means spending the
    bank to nothing and losing every ship:

    1. convert the opening ship, so the seat owns a shipyard;
    2. spawn on consecutive turns while the bank can afford two more: the ship
       spawned last turn is still standing on the yard, so the pair ends the
       turn on one cell with equal (zero) cargo and **both** are destroyed —
       500 halite and one ship gone per turn, no ship left over;
    3. spend the last 500 on one more ship, and
    4. walk that ship onto an enemy shipyard, which destroys the yard **and**
       the ship. The seat now has no ships, a bank of 0 and its own yard still
       standing: eliminated, with assets.
    """
    bank, yards, ships = sim.players[seat]
    ship_ids = list(ships)
    if not yards:
        return {ship_ids[0]: "CONVERT"} if ship_ids else {}
    yard_ids = list(yards)
    if bank >= 2 * SPAWN_COST:
        return {yard_ids[0]: "SPAWN"}
    if not ship_ids:
        return {yard_ids[0]: "SPAWN"} if bank >= SPAWN_COST else {}
    my_cells = {int(p) for p in yards.values()}
    enemy_yard_cells = sorted(
        int(p)
        for other in range(sim.num_seats)
        if other != seat
        for p in sim.players[other][1].values()
        if int(p) not in my_cells
    )
    if not enemy_yard_cells:
        return {}
    sid = ship_ids[0]
    pos = int(ships[sid][0])
    target = min(enemy_yard_cells, key=lambda cell: (micro.dist(pos, cell, sim.size), cell))
    direction = _step_toward(pos, target, sim.size)
    return {sid: direction} if direction else {}


def _raze_orders(sim, seat: int, target: int) -> dict[str, str]:
    """Walk ``seat``'s nearest ship onto ``target`` — an abandoned shipyard."""
    ships = sim.players[seat][2]
    if not ships:
        return {}
    sid = min(
        ships, key=lambda s: (micro.dist(int(ships[s][0]), target, sim.size), s)
    )
    direction = _step_toward(int(ships[sid][0]), target, sim.size)
    return {sid: direction} if direction else {}


def elimination_stream_step(sim, rng: random.Random) -> list[dict[str, str]]:
    """One turn of the elimination stream (see the module docstring)."""
    orders = order_stream_step(sim, rng)
    victim_yards = sim.players[VICTIM_SEAT][1]
    if sim.eliminated[VICTIM_SEAT] is None:
        orders[VICTIM_SEAT] = _victim_orders(sim, VICTIM_SEAT)
    elif victim_yards:
        # The victim is out and its yard is still standing: ram it, so the
        # gate compares the raze that only happens if the yard survived the
        # elimination.
        target = min(int(p) for p in victim_yards.values())
        orders[RAIDER_SEAT] = _raze_orders(sim, RAIDER_SEAT, target)
    return orders
