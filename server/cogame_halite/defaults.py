"""Server-contract defaults, aliases, colours and the rule-constant mirror.

The **rule** constants (`size`, `spawnCost`, `convertCost`, `moveCost`,
`collectRate`, `regenRate`, `maxCellHalite`) are not knobs. They are read from
the **vendored** ``halite.json`` at import time by :func:`upstream_defaults`, so
a re-vendor that changes one fails ``tests/test_vendor.py`` loudly instead of
silently changing the game. Everything in this module that mirrors one of them
is asserted equal to the vendored value by that test.

Server-contract values (deadlines, the strike rule, the wall-clock budget, the
directive cadence) live here and nowhere else.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------
# Seats, aliases, colours
# --------------------------------------------------------------------------

#: Exactly four seats, every variant and the certification fixture.
NUM_SEATS = 4

#: In-game identities. A seat never learns any real player name
#: (``tests/test_privacy.py``); real names live only in ``results.names``, the
#: replay header's ``names``, the scorebug plates and the endcard.
ALIASES: tuple[str, ...] = (
    "FLEET-ALPHA",
    "FLEET-BRAVO",
    "FLEET-CHARLIE",
    "FLEET-DELTA",
)

#: Fixed by seat: amber, teal, magenta, lime — four hues that stay distinct on
#: the dark seabed at 360 px.
COLORS: tuple[str, ...] = ("#e8a33d", "#3fb6b0", "#c65fa8", "#8fbf3f")

# --------------------------------------------------------------------------
# Upstream rule constants (mirrored from the vendored halite.json)
# --------------------------------------------------------------------------

_VENDORED_HALITE_JSON = (
    Path(__file__).resolve().parents[2]
    / "vendor"
    / "upstream"
    / "kaggle_environments"
    / "envs"
    / "halite"
    / "halite.json"
)


@lru_cache(maxsize=1)
def upstream_spec() -> dict:
    """The vendored ``halite.json`` specification, parsed."""
    return json.loads(_VENDORED_HALITE_JSON.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def upstream_defaults() -> dict:
    """``{configKey: default}`` for every scalar-default config field."""
    out: dict[str, object] = {}
    for key, value in upstream_spec()["configuration"].items():
        if isinstance(value, dict):
            if "default" in value:
                out[key] = value["default"]
        else:
            out[key] = value
    return out


BOARD_SIZE = 21
EPISODE_STEPS = 400
STARTING_HALITE = 24000
#: ``reward.default`` — what ``populate_board`` writes as each seat's opening bank.
STARTING_BANK = 5000
SPAWN_COST = 500
CONVERT_COST = 500
MOVE_COST = 0.0
COLLECT_RATE = 0.25
REGEN_RATE = 0.02
MAX_CELL_HALITE = 500

#: The four opening cells (``populate_board``'s 4-agent branch on a 21 board).
STARTING_POSITIONS = (110, 120, 320, 330)

SHIP_ACTIONS = ("NORTH", "SOUTH", "EAST", "WEST", "CONVERT")
SHIPYARD_ACTIONS = ("SPAWN",)
#: Everything ``action.enum`` allows. An unknown value is dropped, never bound.
ALL_ACTIONS = ("CONVERT", "SPAWN", "NORTH", "SOUTH", "EAST", "WEST")

# --------------------------------------------------------------------------
# Server contract
# --------------------------------------------------------------------------

DEFAULT_DIRECTIVE_EVERY = 20
DEFAULT_TURN_DEADLINE_MS = 400
DEFAULT_DIRECTIVE_DEADLINE_MS = 18_000
DEFAULT_DIRECTIVE_SPACING_MS = 10_000
DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS = 120.0
#: Hard stop -> ``reason = "deadline"``. 55% of the assumed 1200 s
#: ``episodeTimeoutSeconds``; the budget guard below should always beat it.
DEFAULT_WALL_CLOCK_BUDGET_SECONDS = 660.0
#: Stop asking players anything past this; every seat plays the in-process
#: ``tidewalker`` compile and the episode still ends ``complete``.
DEFAULT_BUDGET_GUARD_SECONDS = 600.0

#: Consecutive server-side substitutions that mark a seat dead. A valid reply
#: revives it.
STRIKE_LIMIT = 10

#: The manifest's top-level ``episode_timeout_minutes``. The degrade pin is to
#: settle inside 60% of it.
PLATFORM_EPISODE_TIMEOUT_MINUTES = 20

# --------------------------------------------------------------------------
# Caps. Every string that can reach the replay is truncated on RUNE
# boundaries, never byte boundaries.
# --------------------------------------------------------------------------

MAX_ACTIONS_PER_TURN = 256
MAX_ASSET_ID_CHARS = 24
MAX_NOTE_RUNES = 140
MAX_LABEL_RUNES = 40
MAX_STOP_DETAIL_RUNES = 200
MAX_FALLBACK_DETAIL_RUNES = 120
MAX_STRATEGY_RUNES = 2000

SOURCES = ("llm", "retry", "scripted", "fallback")
INTENTS = ("mine", "expand", "raid", "defend", "hold")
FALLBACK_CAUSES = ("timeout", "malformed", "wrong_turn", "disconnected", "host_error")

REASONS = ("complete", "deadline", "fault")
END_RULES = ("full_time", "last_fleet", "wall_clock", "fault")

BASELINES = ("tidewalker", "corsair")
DEFAULT_BASELINE = "tidewalker"


def truncate_runes(text: object, limit: int) -> str:
    """Truncate on a **rune** boundary.

    A byte-boundary truncation splits a multi-byte character and produces
    replay bytes that render in a browser but fail a strict UTF-8 parser
    (``bytes.decode("utf-8")``). Python strings are already sequences of code
    points, so slicing is rune-safe — but going through this one function is
    what makes that a property of the codebase rather than an accident, and it
    also drops lone surrogates that ``json.loads`` would happily accept and
    ``str.encode("utf-8")`` would then reject.
    """
    if text is None:
        return ""
    s = str(text)
    s = s.encode("utf-8", "replace").decode("utf-8", "replace")
    if len(s) > limit:
        s = s[:limit]
    return s
