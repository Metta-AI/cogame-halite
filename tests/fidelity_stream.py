"""The random-but-legal order stream both sides of the fidelity gate replay.

Generated from OUR sim's state and recorded; the upstream reference replays the
recorded stream. Two properties make it a usable gate rather than a lottery:

* **Nothing is elided.** Ships move, mine, convert and collide with each other
  freely, so the ram rule, the leftover-convert hold-aside, deposits,
  regeneration and uid minting are all exercised at every turn.
* **No seat can be eliminated**, so the episode reaches the full 399 turns and
  the gate's tick-count floor is real. Two rules buy that: every seat converts
  its opening ship (so it always owns a shipyard) and no ship is ever ordered
  onto **another seat's** shipyard, which is the only way a shipyard can be
  destroyed. Banks are kept above the spawn cost.
"""

from __future__ import annotations

import random

from cogame_halite import micro

MOVES = ("NORTH", "SOUTH", "EAST", "WEST")

#: Never spend the bank below this: a seat with no ships and a bank under the
#: spawn cost is eliminated at the end of the turn.
BANK_FLOOR = 2000


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
