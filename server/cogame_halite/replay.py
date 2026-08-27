"""The replay document — self-sufficient by construction.

One UTF-8 JSON document (extension ``.replay``), written once at the end and
streamable turn by turn. **Everything the viewer needs is in the bytes**: real
player names, aliases, colours, the full config, the seed and the complete
per-turn state. The viewer contacts S3 and nothing else.

The per-turn ``halite`` array is rounded to integers because that is what is
drawn; the exact float state is pinned by ``hash``, and by ``orders`` + ``seed``
which let CI re-derive the episode exactly (``tests/test_replay.py``).

``stop`` is one load-bearing record applied by the same code on record and on
re-derive — a wall-clock stop is a wall-clock fact that cannot be recomputed
from sim state (the particle-worlds 2026-08-26 scar).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import defaults, events
from .version import GAME_VERSION, PROTOCOL, REPLAY_FORMAT, REPLAY_VERSION


class ReplayError(ValueError):
    """A malformed replay document."""


@dataclass
class ReplayWriter:
    coworld: str
    seed: int
    config: dict[str, Any]
    names: list[str]
    aliases: list[str]
    policy_sources: list[str]
    colors: list[str]
    turns: list[dict] = field(default_factory=list)
    results: dict | None = None
    stop: dict | None = None

    def add_turn(
        self,
        turn: int,
        state: dict,
        orders: list[dict[str, str]],
        turn_events: list[dict],
        state_hash: str,
    ) -> None:
        for event in turn_events:
            events.validate(event)
        self.turns.append(
            {
                "t": int(turn),
                "halite": list(state["halite"]),
                "players": state["players"],
                "orders": [dict(o) for o in orders],
                "events": list(turn_events),
                "hash": state_hash,
            }
        )

    def set_stop(self, rule: str, turn: int) -> dict:
        """Record the stop. Same constructor on record and on re-derive."""
        self.stop = {k: v for k, v in events.stop(rule, turn).items() if k != "k"}
        return self.stop

    def document(self) -> dict[str, Any]:
        if self.stop is None:
            raise ReplayError("replay has no stop record — set_stop() was never called")
        return {
            "format": REPLAY_FORMAT,
            "version": REPLAY_VERSION,
            "gameVersion": GAME_VERSION,
            "protocol": PROTOCOL,
            "coworld": self.coworld,
            "seed": int(self.seed),
            "config": self.config,
            "names": list(self.names),
            "aliases": list(self.aliases),
            "policySources": list(self.policy_sources),
            "colors": list(self.colors),
            "turns": self.turns,
            "results": self.results,
            "stop": self.stop,
        }

    def to_bytes(self) -> bytes:
        """Strict UTF-8 JSON. ``ensure_ascii=False`` keeps notes readable; the
        rune-boundary truncation in ``defaults.truncate_runes`` is what makes
        that safe for a strict parser."""
        return json.dumps(
            self.document(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


REQUIRED_KEYS = (
    "format",
    "version",
    "gameVersion",
    "protocol",
    "coworld",
    "seed",
    "config",
    "names",
    "aliases",
    "policySources",
    "colors",
    "turns",
    "results",
    "stop",
)


def parse(raw: bytes | str) -> dict[str, Any]:
    """Parse and validate a replay document (strict UTF-8, closed shape)."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReplayError(f"replay is not strict UTF-8: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"replay is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ReplayError("replay must be a JSON object")
    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        raise ReplayError(f"replay is missing {missing}")
    if doc["format"] != REPLAY_FORMAT:
        raise ReplayError(f"not a {REPLAY_FORMAT} document: {doc['format']!r}")
    if not isinstance(doc["turns"], list) or not doc["turns"]:
        raise ReplayError("replay has no turns")
    stop = doc["stop"]
    if not isinstance(stop, dict) or stop.get("rule") not in defaults.END_RULES:
        raise ReplayError(f"replay stop record is malformed: {stop!r}")
    for turn in doc["turns"]:
        for event in turn.get("events", []):
            events.validate(event)
    return doc
