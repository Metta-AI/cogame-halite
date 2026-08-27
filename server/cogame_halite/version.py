"""Game version and wire protocol identifier.

``GAME_VERSION`` is a **claim, not a counter** (the cogame-factorio rule):
anything that changes what a policy observes or how a seat is scored bumps it
in the same commit, with a prepend-only changelog entry in the shape
``GVnn (short rule name): HEADLINE``.

Changelog (newest first):

  GV1 (initial port): Halite IV at kaggle-environments 1.32.7, 21x21 torus,
      four seats, 400 turns, directive-every-20 LLM cadence, closed results
      schema, FNV-1a state hash, static wasm replay viewer.
"""

from __future__ import annotations

#: Plain ``MAJOR.MINOR.PATCH`` — the manifest and the replay header carry it.
GAME_VERSION = "1.0.0"

#: The wire protocol name policies see in ``hello`` and the replay header.
PROTOCOL = "halite/1"

#: Replay document format id and version.
REPLAY_FORMAT = "cogame-halite-replay"
REPLAY_VERSION = 1
