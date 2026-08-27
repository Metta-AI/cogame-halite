"""LLM transport, the retry ladder and directive repair.

One directive per directive turn — a 20-turn plan the local micro layer then
executes every turn. That is the Kaggle-proven pattern the design note names,
and it is what makes 20 LLM batches cover 1 600 asset-turns.

Providers, in order of preference:

* **Bedrock** (``USE_BEDROCK=true``): the platform attaches a Bedrock sidecar to
  the player pod. Every LLM policy's ``env`` in ``tools/ci/policies.json`` must
  carry ``USE_BEDROCK: "true"`` — ``PLAYER_PROMPT`` alone gets no sidecar and
  the seat silently plays scripted (the cogolf 2026-08-24 scar).
* **Anthropic API** (``ANTHROPIC_API_KEY``).

Model ``us.anthropic.claude-haiku-4-5-20251001-v1:0``, ``max_tokens`` **900**
(400 truncates — ``cut off at max_tokens``), **no** ``output_config.effort``
(Haiku 4.5 rejects it).

Degrade, never hang: attempt 1 has a 12 s client-side timeout, the single retry
has 5 s and a shortened prompt and logs ``will retry`` (never ``falling back``
— the phase-60 grep distinguishes them), and if the retry also fails the player
keeps its previous directive and answers **within the deadline**.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from typing import Any

from cogame_halite import defaults
from cogame_halite.micro import Directive, TIDEWALKER

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MAX_TOKENS = 900
ATTEMPT1_TIMEOUT_SECONDS = 12.0
RETRY_TIMEOUT_SECONDS = 5.0

SYSTEM_PROMPT = (
    "You command a fleet in Halite IV: four fleets mine a 21x21 wrap-around "
    "board and steal each\nother's cargo. You are given the whole board; "
    "nothing is hidden. Reply with ONE JSON object and\nnothing else. Your "
    "reply MUST begin with the character { and end with }. No prose, no "
    "markdown,\nno code fences, no explanation outside the JSON."
)

STANCES = ("expand", "mine", "raid", "defend")
FOCI = ("NW", "NE", "SW", "SE", "CENTER")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def build_prompt(
    observation: dict,
    strategy: str,
    last_directive: Directive | None,
    *,
    short: bool = False,
) -> str:
    from cogame_halite import micro

    view = micro.BoardView.from_observation(observation)
    seat = int(observation.get("seat", observation.get("player", 0)))
    aliases = list(observation.get("aliases") or defaults.ALIASES)
    alias = observation.get("alias") or aliases[seat]
    turn = view.turn
    max_turns = view.max_turns
    size = view.size
    bank, yards, ships = view.players[seat]
    cargo_afloat = sum(int(v[1]) for v in ships.values())

    standings = " | ".join(
        f"{aliases[i]} {int(view.players[i][0])}" for i in range(len(view.players))
    )

    def cell_text(pos: int) -> str:
        x, y = micro.xy(pos, size)
        return f"({x},{y})"

    ship_lines = []
    for i, (sid, (pos, cargo)) in enumerate(sorted(ships.items())):
        if i >= 24:
            ship_lines.append(f"+{len(ships) - 24} more")
            break
        ship_lines.append(f"{sid}@{cell_text(int(pos))} cargo {int(cargo)}")
    yard_lines = [f"{yid}@{cell_text(int(pos))}" for yid, pos in sorted(yards.items())]

    threats = 0
    lightest = None
    for other in range(len(view.players)):
        if other == seat:
            continue
        for pos, cargo in view.players[other][2].values():
            for mine_pos, mine_cargo in ships.values():
                if int(mine_cargo) <= 0:
                    continue
                if micro.dist(int(pos), int(mine_pos), size) <= 2:
                    threats += 1
                    if lightest is None or int(cargo) < lightest:
                        lightest = int(cargo)
                    break

    last = json.dumps(last_directive.as_dict()) if last_directive else "none"
    strategy = defaults.truncate_runes(strategy, defaults.MAX_STRATEGY_RUNES)
    directive_every = int(observation.get("directiveEvery") or defaults.DEFAULT_DIRECTIVE_EVERY)
    board = observation.get("board", "")
    if short:
        board = "\n".join(board.splitlines()[:0]) or "(omitted on the retry)"

    return f"""TURN {turn}/{max_turns} - DIRECTIVE TURN (this plan stands for the next {directive_every} turns)
YOU ARE {alias}   BANK {int(bank)}   SHIPS {len(ships)}   YARDS {len(yards)}   CARGO AFLOAT {cargo_afloat}
STANDINGS (banked)  {standings}
BOARD  lower-case = ship, UPPER-CASE = shipyard, digit = that cell's halite on a 0-9 scale of 500
{board}
LEGEND  {"  ".join(f"{chr(ord('a') + i)}/{chr(ord('A') + i)}={aliases[i]}" for i in range(len(aliases)))}
YOUR SHIPS  {"; ".join(ship_lines) or "none"}
YOUR YARDS  {"; ".join(yard_lines) or "none"}
THREATS  {threats} enemy ships within 2 cells of one of your loaded ships; lightest nearby enemy cargo {lightest if lightest is not None else "n/a"}
RULES THAT DECIDE THIS GAME
- Holding still mines 25% of the cell (rounded down). Moving is free. Cells under a ship do not regrow.
- Two ships on one cell: the LIGHTER one survives and takes the other's cargo. Equal cargo kills both.
- An enemy ship entering your shipyard destroys the shipyard and itself.
- SPAWN costs 500 from the bank. CONVERT costs 500 and turns that ship into a shipyard.
- Cargo scores only when a ship ends its move on YOUR shipyard. Most banked halite at turn {max_turns} wins.
LAST DIRECTIVE  {last}
YOUR STANDING ORDERS  {strategy}
Reply exactly this JSON shape:
{{"stance":"expand|mine|raid|defend","spawnUntil":<int 0-400>,"yards":<int 1-4>,
 "mineFloor":<int 0-500>,"returnAt":<int 50-1500>,"focus":"NW|NE|SW|SE|CENTER",
 "avoid":"<one alias or null>","note":"<<=140 chars, spectator-facing, what you are doing>"}}"""


# --------------------------------------------------------------------------
# Directive repair — never reject, always clamp
# --------------------------------------------------------------------------


def extract_json(text: str) -> dict | None:
    """First balanced ``{...}`` object in ``text``; trailing prose tolerated."""
    if not text:
        return None
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                    except ValueError:
                        start = -1
                        continue
                    return parsed if isinstance(parsed, dict) else None
    return None


def _clamp_int(value: Any, lo: int, hi: int, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(lo, min(hi, int(value)))


def repair(raw: dict, previous: Directive, aliases: list[str], seat: int, max_turns: int) -> Directive:
    """Clamp a reply into the directive schema. Never rejects."""
    prev = previous or TIDEWALKER

    stance = raw.get("stance")
    stance = stance.lower() if isinstance(stance, str) else None
    if stance not in STANCES:
        stance = prev.stance

    focus = raw.get("focus")
    focus = focus.upper() if isinstance(focus, str) else None
    if focus not in FOCI:
        focus = prev.focus

    avoid = raw.get("avoid")
    opponents = [a for i, a in enumerate(aliases) if i != seat]
    if not isinstance(avoid, str) or avoid not in opponents:
        avoid = None

    return Directive(
        stance=stance,
        spawnUntil=_clamp_int(raw.get("spawnUntil"), 0, max_turns, prev.spawnUntil),
        yards=_clamp_int(raw.get("yards"), 1, 4, prev.yards),
        mineFloor=_clamp_int(raw.get("mineFloor"), 0, 500, prev.mineFloor),
        returnAt=_clamp_int(raw.get("returnAt"), 50, 1500, prev.returnAt),
        focus=focus,
        avoid=avoid,
        note=defaults.truncate_runes(raw.get("note", ""), defaults.MAX_NOTE_RUNES),
    )


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class LLMUnavailable(RuntimeError):
    """No provider is configured or reachable."""


class Provider:
    """Thin Bedrock / Anthropic client. Imports its SDK lazily."""

    def __init__(self) -> None:
        self.use_bedrock = str(os.environ.get("USE_BEDROCK", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        self._kind = ""

    @property
    def available(self) -> bool:
        return bool(self.use_bedrock or self.api_key)

    def _ensure(self):
        if self._client is not None:
            return self._client
        if self.use_bedrock:
            from anthropic import AnthropicBedrock  # noqa: PLC0415

            self._client = AnthropicBedrock()
            self._kind = "bedrock"
        elif self.api_key:
            from anthropic import Anthropic  # noqa: PLC0415

            self._client = Anthropic(api_key=self.api_key)
            self._kind = "anthropic"
        else:
            raise LLMUnavailable("no LLM provider configured")
        return self._client

    def _call(self, prompt: str) -> str:
        client = self._ensure()
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        if getattr(message, "stop_reason", "") == "max_tokens":
            log("LLM reply was cut off at max_tokens")
        parts = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    async def complete(self, prompt: str, timeout: float) -> str:
        """One bounded call. Raises on timeout or provider error."""
        return await asyncio.wait_for(asyncio.to_thread(self._call, prompt), timeout)


class DirectiveClient:
    """The retry ladder around one :class:`Provider`."""

    def __init__(self, strategy: str, provider: Provider | None = None) -> None:
        self.strategy = strategy
        self.provider = provider or Provider()
        self.last: Directive = TIDEWALKER
        self.last_latency_ms = 0

    @property
    def available(self) -> bool:
        return self.provider.available

    async def directive(self, observation: dict) -> tuple[Directive, str, str]:
        """Return ``(directive, source, note_cause)``.

        ``source`` is ``llm`` (attempt 1), ``retry`` (the single retry) or
        ``scripted`` (both failed — the previous directive stands).
        """
        aliases = list(observation.get("aliases") or defaults.ALIASES)
        seat = int(observation.get("seat", 0))
        max_turns = int(observation.get("maxTurns") or defaults.EPISODE_STEPS)
        if not self.available:
            return self.last, "scripted", "no LLM provider configured"

        started = time.monotonic()
        for attempt, (timeout, source) in enumerate(
            ((ATTEMPT1_TIMEOUT_SECONDS, "llm"), (RETRY_TIMEOUT_SECONDS, "retry"))
        ):
            prompt = build_prompt(observation, self.strategy, self.last, short=attempt > 0)
            try:
                text = await self.provider.complete(prompt, timeout)
                raw = extract_json(text)
                if raw is None:
                    raise ValueError("no JSON object in the reply")
            except Exception as exc:  # noqa: BLE001 - any provider failure
                cause = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    log(f"LLM attempt 1 failed ({cause}); will retry")
                    continue
                log(f"LLM retry failed ({cause}); falling back to the last directive")
                self.last_latency_ms = int((time.monotonic() - started) * 1000)
                return self.last, "scripted", cause
            self.last = repair(raw, self.last, aliases, seat, max_turns)
            self.last_latency_ms = int((time.monotonic() - started) * 1000)
            return self.last, source, ""
        return self.last, "scripted", "exhausted"  # pragma: no cover
