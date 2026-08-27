"""The replay event vocabulary and its constructors.

One ``events`` array per turn, each ``{"k": <kind>, ...}``, emitted in the
order the resolution produced them. This module is the **complete** vocabulary:
``tests/test_replay.py`` schema-checks every kind against :data:`EVENT_SCHEMA`,
and ``tests/test_viewer.py`` asserts the viewer has a ``.beat-marker.<kind>``
CSS rule for every kind in :data:`BEAT_KINDS`.

Sim-derived kinds (``spawn``, ``convert``, ``deposit``, ``mine``, ``collide``,
``yardraze``, ``eliminate``) are built in ``sim.py`` from before/after state.
The kinds below are wall-clock or transport facts the engine owns.
"""

from __future__ import annotations

from typing import Any

from . import defaults

#: kind -> the exact key set its payload carries (besides ``k``).
EVENT_SCHEMA: dict[str, tuple[str, ...]] = {
    "spawn": ("seat", "ship", "pos"),
    "convert": ("seat", "ship", "yard", "pos"),
    "deposit": ("seat", "ship", "yard", "pos", "amount"),
    "mine": ("seat", "ship", "pos", "amount"),
    "collide": ("pos", "survivor", "lost", "stolen"),
    "yardraze": ("pos", "yardSeat", "yard", "shipSeat", "ship"),
    "eliminate": ("seat", "turn"),
    "lead": ("seat", "bank"),
    "note": ("seat", "text", "source", "latencyMs"),
    "fallback": ("seat", "cause", "detail"),
    "strike": ("seat",),
    "budget_guard": ("turn",),
    "stop": ("rule", "turn"),
}

#: Kinds that place a clickable, labelled beat on the scrubber, with the label
#: the viewer draws. Every one needs a ``.beat-marker.<kind>`` CSS rule.
BEAT_KINDS: dict[str, str] = {
    "convert": "yard",
    "collide": "ram",
    "yardraze": "raze",
    "eliminate": "out",
    "lead": "lead",
    "guard": "guard",
}


def lead(seat: int, bank: int) -> dict[str, Any]:
    """The leader changed. Emitted only on a change."""
    return {"k": "lead", "seat": int(seat), "bank": int(bank)}


def note(seat: int, text: str, source: str, latency_ms: int) -> dict[str, Any]:
    """A seat's spectator-facing line. Rune-truncated to 140."""
    return {
        "k": "note",
        "seat": int(seat),
        "text": defaults.truncate_runes(text, defaults.MAX_NOTE_RUNES),
        "source": source if source in defaults.SOURCES else "scripted",
        "latencyMs": max(0, int(latency_ms)),
    }


def fallback(seat: int, cause: str, detail: str = "") -> dict[str, Any]:
    """A server-side substitution. ``cause`` partitions ``results.fallbacks``."""
    return {
        "k": "fallback",
        "seat": int(seat),
        "cause": cause if cause in defaults.FALLBACK_CAUSES else "host_error",
        "detail": defaults.truncate_runes(detail, defaults.MAX_FALLBACK_DETAIL_RUNES),
    }


def strike(seat: int) -> dict[str, Any]:
    """The strike rule marked a seat dead."""
    return {"k": "strike", "seat": int(seat)}


def budget_guard(turn: int) -> dict[str, Any]:
    """The budget guard fired: no seat is asked anything from here on."""
    return {"k": "budget_guard", "turn": int(turn)}


def stop(rule: str, turn: int) -> dict[str, Any]:
    """The load-bearing stop record.

    A wall-clock stop is a wall-clock fact that cannot be recomputed from sim
    state (the particle-worlds 2026-08-26 scar), so it is **recorded**, not
    inferred, and applied by this same constructor on record and on re-derive.
    """
    if rule not in defaults.END_RULES:
        raise ValueError(f"unknown end rule: {rule!r}")
    return {"k": "stop", "rule": rule, "turn": int(turn)}


def validate(event: dict) -> None:
    """Raise if ``event`` is not exactly one of the declared shapes."""
    kind = event.get("k")
    if kind not in EVENT_SCHEMA:
        raise ValueError(f"unknown event kind: {kind!r}")
    expected = set(EVENT_SCHEMA[kind])
    actual = set(event) - {"k"}
    if actual != expected:
        raise ValueError(
            f"event {kind!r} keys {sorted(actual)} != schema {sorted(expected)}"
        )
