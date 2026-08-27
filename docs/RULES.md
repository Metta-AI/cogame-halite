# Rules & bit-exactness

**Four fleets, one salt flat, four hundred turns.** cogame-halite is a
bit-exact port of **Kaggle's Halite IV** (`kaggle-environments`, Two Sigma
seasons I–IV; the shipped env is season IV) onto the Coworld platform.

## The game in one paragraph

Every fleet starts with one ship and 5 000 banked halite in its own quadrant of
a 21 × 21 wrap-around board holding about 24 000 halite in uneven clusters. A
ship that holds still scrapes **25 %** of the halite under it into its hold. A
ship that moves carries its hold with it — and a hold is a target: when two
ships end a turn on the same cell, the one carrying **less** survives and
**takes the other's cargo**, and if they carry exactly the same, both are
destroyed. Cargo only becomes score when a ship ends its move on one of its own
shipyards. You get shipyards by burning a ship and 500 halite (`CONVERT`); you
get ships by burning 500 banked halite at a shipyard (`SPAWN`). An enemy ship
that walks onto your shipyard destroys the shipyard and itself. Everyone moves
at once, every turn, with no information hidden.

The tension: halite in a hold is worth nothing and is *stealable*; halite in
the bank is worth everything and can never be lost. Mining longer earns more
but makes you heavier, and heavy loses every collision.

## Rule constants

All upstream defaults, all served unchanged, all **read from the vendored
`halite.json`** rather than re-typed (`server/cogame_halite/defaults.py`):

| Constant | Value | Upstream key |
|---|---|---|
| Board edge | 21 (441 cells, wraps in both axes) | `size` |
| Turns in an episode | 400 | `episodeSteps` |
| Board halite at turn 0 | 24 000 | `startingHalite` |
| Bank per fleet at turn 0 | 5 000 | `reward.default` |
| Mine rate | 0.25 of the cell, `int()`-truncated | `collectRate` |
| Regeneration rate | 0.02 per turn, on cells with no ship | `regenRate` |
| Cell cap | 500 | `maxCellHalite` |
| Ship cost | 500 | `spawnCost` |
| Convert cost | 500 | `convertCost` |
| Move cost | 0.0 | `moveCost` |
| Actions | `NORTH`, `SOUTH`, `EAST`, `WEST`, `CONVERT` (ships), `SPAWN` (shipyards), plus *no action* = mine | `action.enum` |

`size`, `spawnCost`, `convertCost`, `moveCost`, `collectRate`, `regenRate` and
`maxCellHalite` are **pinned** in `config_schema` (min == max == the upstream
default), so a variant cannot drift them. Only `episode_steps` and
`starting_halite` vary between variants, and both are first-class upstream
config fields — the rules stay bit-exact in every variant.

## Geometry

* A cell is `Point(x, y)` with `x` rightwards and **`y` upwards**;
  `index = (size - y - 1) * size + x`. **Index 0 is the top-left cell on
  screen** and index 440 the bottom-right, so the viewer draws the `halite`
  array in raster order and gets upstream's own orientation for free.
* `NORTH = (0, +1)` = `index - size` = **up on screen**; `SOUTH` down; `EAST`
  `+1`; `WEST` `-1`; all four wrap.
* Distances are torus Manhattan.
* Asset ids are strings minted `f"{turn}-{n}"`, `n` counting from 1 across
  **all** players within one resolution. The four opening ships are `0-1`,
  `0-2`, `0-3`, `0-4`.
* `populate_board` mirrors one 11 × 11 quartile into all four quadrants, so the
  board is exactly 4-fold symmetric and the four starting cells are equivalent
  by construction: seat 0 at index **110** = (5, 15), seat 1 at **120** =
  (15, 15), seat 2 at **320** = (5, 5), seat 3 at **330** = (15, 5). No seat
  can be dealt a better island, so no seat randomisation is applied.

## Turn resolution — the exact order

One call of `sim.step(orders)` is the vendored `Board.next()` followed by the
elimination block of `interpreter()`. **This is not a re-implementation: the
vendored `Board` class is imported and called.** The order is written out so a
reader knows what the game *is*, and so `tests/test_sim.py` has a numbered list
to assert against.

1. **Bind actions.** Each ship and shipyard takes at most one action. A value
   not in the enum, or an id the seat does not own, is simply **not bound**.
   `SPAWN` on a ship and a direction on a shipyard are likewise not bound.
2. **Per-seat action processing, seats in ascending seat order.**
   1. **`SPAWN`**, shipyards in insertion order. If `bank >= 500`:
      `bank -= 500` and a new ship with 0 cargo appears on that cell. A seat
      that orders more spawns than it can pay for gets the ones its bank
      covers, in that order.
   2. **`CONVERT`**, ships in insertion order. Allowed only if the cell holds
      **no shipyard** (of anyone) and `ship.cargo + bank >= 500`. Then
      `delta = cargo - 500`; `bank += min(delta, 0)` immediately;
      `max(delta, 0)` is **held aside** and added only after all of this seat's
      converts (upstream's guard against chaining one convert into another); a
      shipyard appears, **the cell's halite is set to 0**, the ship is deleted.
   3. **Moves.** One cell with wrap. `moveCost` is 0, so cargo is unchanged.
      Two ships may land on the same cell — that is the point.
   4. `bank += leftover_convert_halite`, then `assert bank >= 0`.
3. **Ship-to-ship collisions**, over every cell holding more than one ship. The
   **strictly smallest cargo survives** and **absorbs the cargo of every ship
   destroyed there**. A tie for smallest destroys them all. Ownership is
   irrelevant: friendly ships collide on exactly the same rule.
4. **Ship-to-shipyard collisions.** A ship of a **different** seat standing on
   a shipyard destroys both.
5. **Deposit.** A surviving shipyard with its **own** seat's ship on it banks
   that ship's cargo and zeroes it. This is the only way halite is ever banked.
6. **Mining.** `delta = int(cell.halite * 0.25)`. A ship mines iff its bound
   action was **not** a move, the cell holds **no shipyard**, and `delta > 0`.
   Cargo is uncapped.
7. **Regeneration.** For every cell **with no ship on it**:
   `halite = min(round(halite * 1.02, 3), 500)`. The `round(x, 3)` is Python's
   round-half-to-even and is part of the contract.
8. **Turn counter.** `turn += 1`.
9. **Elimination.** A seat still active whose ships are all gone **and** which
   either has no shipyard or has `bank < 500` is eliminated at this turn: it
   can never act again and its score freezes at `turn - episode_steps - 1`.
10. **Last-fleet check.** Fewer than two active seats ends the episode.

Things a reader will otherwise get wrong, each an upstream fact:

* Spawn is processed **before** convert within a seat, and a seat's whole block
  before the next seat's — but no seat sees another's actions, so the ordering
  only decides how a *single* seat's bank is spent.
* Collisions resolve **after all movement**, so head-on swaps (A→B, B→A) do
  **not** collide in Halite IV. There is no swap rule.
* Deposit happens **after** collisions: a loaded ship rammed on the doorstep of
  its own shipyard loses everything.
* A cell that becomes a shipyard loses its halite permanently.
* A ship standing still suppresses that cell's regeneration *and* mines it.

## Scoring

```
score[s] = banked[s]                        if eliminated[s] is null
         = eliminated[s] - episode_steps - 1  otherwise   (negative)
```

**Higher is better.** A surviving fleet always outranks an eliminated one, and
among eliminated fleets, surviving longer ranks higher — Kaggle's own
leaderboard rule. `results.placement[s]` ∈ {1,2,3,4} uses this ladder, first
difference deciding:

1. `score` descending
2. `shipyards + ships` at the end, descending
3. `mined` (lifetime halite scraped) descending
4. seat index ascending (a total order, so it always terminates)

Seats still equal after rule 3 share the higher `placement` (1,1,3,4); rule 4
still gives `results.ranking` a strict order. `results.win[s]` is
`placement[s] == 1`; `results.winner` is the single seat with `placement == 1`,
or `null` when two or more share it. A `deadline` episode is scored by the same
formula at the turn the clock stopped — never zeroed, always rankable.

## End conditions

| `end_rule` | When | `results.reason` |
|---|---|---|
| `full_time` | The state at the last turn has been recorded. | `complete` |
| `last_fleet` | Fewer than two seats remain active. | `complete` |
| `wall_clock` | `wall_clock_budget_seconds` (660 s) reached at a turn boundary. | `deadline` |
| `fault` | An unhandled exception in the sim or the loop. | `fault` |

`results.reason` is a closed enum of exactly those three values. A seat that
never connects, disconnects, or fails every reply **does not end the episode**:
its fleet plays the server-side `tidewalker` fallback and the game runs to its
natural end.

## Why this is bit-exact, and how you can check

The port **imports** the vendored upstream modules and calls
`Board(obs, config, actions).next()`. `vendor/upstream/` is byte-pristine with
a sha256 per file; `sim/assemble.py` copies it into `build/khalite/` and adds
three package `__init__.py` files and nothing else. Two upstream code paths are
reached differently and are named in `vendor/PATCHES.md` with their citations:
the `populate_board` adapter and the 12-line elimination block.

`tests/test_fidelity.py` is the acceptance criterion and a permanent CI gate.
With `kaggle-environments==1.32.7` installed from the CI-only `fidelity`
dependency group it drives upstream's own `make("halite")` env and our sim over
the same order stream for **8 seeds × 399 turns** and asserts equality of the
full observation at every turn — the 441-entry `halite` list element for
element (exact floats), every player's `[bank, shipyards, ships]` including
**dict insertion order**, `step`, and each agent's `status`/`reward` — plus
50-seed board generation and the four starting positions. Any divergence fails.

## Not in v1

Kaggle's HTML replay renderer (this repo ships its own static wasm bundle);
Halite seasons I–III; 1- and 2-player agent counts; `remainingOverageTime` as a
real time bank; the open Kaggle bot corpus as policies; automated
alliance-pattern audit tooling; a live spectator pod of any kind.
