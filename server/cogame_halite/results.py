"""The closed results document, the scoring formula and the placement ladder.

Exactly the keys in :data:`RESULTS_KEYS` — in this module, in the manifest's
``results_schema`` (``additionalProperties: false``) and in
``tools/ci/docker_smoke.sh``'s expected-key set. Three places, one list,
asserted equal by ``tests/test_results.py``.

Scoring (design note §"Scoring formula and sign")::

    score[s] = banked[s]                  if eliminated[s] is None
             = eliminated[s] - episode_steps - 1   otherwise (negative)

Higher is better. A surviving fleet always outranks an eliminated one, and
among eliminated fleets, surviving longer ranks higher — Kaggle's own rule.
"""

from __future__ import annotations

from typing import Any

from . import defaults

#: The closed key set. Order is the document's key order.
RESULTS_KEYS: tuple[str, ...] = (
    "names",
    "aliases",
    "scores",
    "placement",
    "ranking",
    "win",
    "winner",
    "reason",
    "end_rule",
    "final_turn",
    "seed",
    "banked",
    "ships",
    "yards",
    "mined",
    "stolen",
    "collisions_won",
    "collisions_lost",
    "eliminated_turn",
    "llm_turns",
    "fallbacks",
    "dead_seats",
    "stop_detail",
)


def score_of(banked: int, eliminated_turn: int | None, episode_steps: int) -> int:
    """The design note's formula, sign included."""
    if eliminated_turn is None:
        return int(banked)
    return int(eliminated_turn) - int(episode_steps) - 1


def placement_and_ranking(
    scores: list[int],
    assets: list[int],
    mined: list[int],
) -> tuple[list[int], list[int]]:
    """The tie-break ladder, first difference deciding.

    1. ``score``  descending
    2. ``shipyards + ships`` at the end  descending
    3. ``mined`` (lifetime halite scraped)  descending
    4. seat index  ascending  (total order, always terminates)

    ``ranking`` is the strict seat order with rule 4 applied. ``placement``
    gives seats still equal after rule 3 the same (higher) number: 1, 1, 3, 4.
    """
    seats = list(range(len(scores)))
    key = lambda s: (-scores[s], -assets[s], -mined[s], s)  # noqa: E731
    ranking = sorted(seats, key=key)
    tie_key = lambda s: (scores[s], assets[s], mined[s])  # noqa: E731
    placement = [0] * len(scores)
    position = 1
    index = 0
    while index < len(ranking):
        group = [ranking[index]]
        while index + 1 < len(ranking) and tie_key(ranking[index + 1]) == tie_key(group[0]):
            index += 1
            group.append(ranking[index])
        for seat in group:
            placement[seat] = position
        position += len(group)
        index += 1
    return placement, ranking


def build(
    *,
    names: list[str],
    aliases: list[str],
    banked: list[int],
    ships: list[int],
    yards: list[int],
    mined: list[int],
    stolen: list[int],
    collisions_won: list[int],
    collisions_lost: list[int],
    eliminated_turn: list[int | None],
    llm_turns: list[int],
    fallbacks: list[dict[str, int]],
    dead_seats: list[bool],
    reason: str,
    end_rule: str,
    final_turn: int,
    seed: int,
    episode_steps: int,
    stop_detail: str = "",
) -> dict[str, Any]:
    """The full results document. Every key in :data:`RESULTS_KEYS`, no other."""
    if reason not in defaults.REASONS:
        raise ValueError(f"reason must be one of {defaults.REASONS}, got {reason!r}")
    if end_rule not in defaults.END_RULES:
        raise ValueError(f"end_rule must be one of {defaults.END_RULES}, got {end_rule!r}")

    scores = [
        score_of(banked[s], eliminated_turn[s], episode_steps) for s in range(len(banked))
    ]
    assets = [ships[s] + yards[s] for s in range(len(banked))]
    placement, ranking = placement_and_ranking(scores, assets, mined)
    win = [p == 1 for p in placement]
    winners = [s for s, w in enumerate(win) if w]
    winner = winners[0] if len(winners) == 1 else None

    doc: dict[str, Any] = {
        "names": list(names),
        "aliases": list(aliases),
        "scores": scores,
        "placement": placement,
        "ranking": ranking,
        "win": win,
        "winner": winner,
        "reason": reason,
        "end_rule": end_rule,
        "final_turn": int(final_turn),
        "seed": int(seed),
        "banked": [int(b) for b in banked],
        "ships": [int(v) for v in ships],
        "yards": [int(v) for v in yards],
        "mined": [int(v) for v in mined],
        "stolen": [int(v) for v in stolen],
        "collisions_won": [int(v) for v in collisions_won],
        "collisions_lost": [int(v) for v in collisions_lost],
        "eliminated_turn": [None if t is None else int(t) for t in eliminated_turn],
        "llm_turns": [int(v) for v in llm_turns],
        "fallbacks": [
            {cause: int(counts.get(cause, 0)) for cause in defaults.FALLBACK_CAUSES}
            for counts in fallbacks
        ],
        "dead_seats": [bool(v) for v in dead_seats],
        "stop_detail": defaults.truncate_runes(stop_detail, defaults.MAX_STOP_DETAIL_RUNES),
    }
    assert tuple(doc) == RESULTS_KEYS, (tuple(doc), RESULTS_KEYS)
    return doc
