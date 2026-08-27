# cogame-halite

A **four-seat, free-for-all economy race with physical risk on every move**: a
bit-exact Coworld port of **Kaggle's Halite IV** (`kaggle-environments`, Two
Sigma seasons I–IV).

Four fleets mine a 21 × 21 torus of halite, haul it home to their shipyards,
and ram each other — the ship carrying *less* survives the collision and takes
the other's cargo. Most banked halite at turn 400 wins.

* Rules and bit-exactness: [`docs/RULES.md`](docs/RULES.md)
* Wire protocol: [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
* Replay format + viewer contract: [`docs/REPLAY.md`](docs/REPLAY.md)
* Design note: [`docs/plans/2026-08-27-halite-design.md`](docs/plans/2026-08-27-halite-design.md)
* Working conventions: [`AGENTS.md`](AGENTS.md)
