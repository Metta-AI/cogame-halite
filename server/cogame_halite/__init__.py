"""cogame-halite — a bit-exact Coworld port of Kaggle's Halite IV.

Four fleets mine a 21x21 torus of halite, haul it home to their shipyards and
ram each other; the ship carrying *less* survives a collision and takes the
other's cargo. Most banked halite at turn 400 wins.

Turn resolution is the vendored ``kaggle-environments`` code itself
(``vendor/upstream/``, assembled by ``sim/assemble.py``) — not a
transcription. See ``docs/plans/2026-08-27-halite-design.md``.
"""

from .version import GAME_VERSION, PROTOCOL

__all__ = ["GAME_VERSION", "PROTOCOL"]
