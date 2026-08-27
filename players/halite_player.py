"""The one player entrypoint: ``python -m players.halite_player``.

Env-switched, one image, two policy families:

* ``PLAYER_PROMPT=<strategy text>``  -> an **LLM** seat. On a directive turn it
  asks for a directive (``players/llm.py``); on every other turn it answers
  from its compiled plan in milliseconds.
* ``PLAYER_SCRIPTED=tidewalker|corsair`` -> a **scripted** seat.

A seat that sets neither, or sets an unrecognised name, plays ``tidewalker``
(the published default). Both families execute the same micro layer
(``server/cogame_halite/micro.py``), which the server's fallback path also
imports, so the two can never drift.
"""

from __future__ import annotations

import os
import sys
import time

from cogame_halite import defaults, micro

from . import client, llm


class HalitePolicy(client.Policy):
    def __init__(self) -> None:
        prompt = os.environ.get("PLAYER_PROMPT", "").strip()
        scripted = os.environ.get("PLAYER_SCRIPTED", "").strip()
        self.baseline = scripted if scripted in defaults.BASELINES else defaults.DEFAULT_BASELINE
        if scripted and scripted not in defaults.BASELINES:
            client.log(
                f"PLAYER_SCRIPTED={scripted!r} is not one of {defaults.BASELINES}; "
                f"playing {defaults.DEFAULT_BASELINE}"
            )
        self.llm: llm.DirectiveClient | None = None
        if prompt:
            self.llm = llm.DirectiveClient(prompt)
            self.policy_name = "llm"
            self.label = defaults.truncate_runes(
                prompt.splitlines()[0] if prompt else "llm", defaults.MAX_LABEL_RUNES
            )
            if not self.llm.available:
                client.log(
                    "PLAYER_PROMPT is set but no LLM provider is configured "
                    "(USE_BEDROCK / ANTHROPIC_API_KEY); every turn will be scripted"
                )
        else:
            self.policy_name = f"scripted:{self.baseline}"
            self.label = self.baseline
        self.directive = micro.baseline_directive(self.baseline)
        self.seat = 0
        self.directive_every = defaults.DEFAULT_DIRECTIVE_EVERY

    def on_hello(self, hello: dict) -> None:
        self.seat = int(hello.get("seat", 0))
        self.directive_every = int(
            hello.get("directiveEvery") or defaults.DEFAULT_DIRECTIVE_EVERY
        )
        client.log(
            f"seat {self.seat} ({hello.get('alias')}) policy={self.policy_name} "
            f"baseline={self.baseline} directiveEvery={self.directive_every}"
        )

    async def orders(self, observation: dict) -> dict:
        started = time.monotonic()
        seat = int(observation.get("seat", self.seat))
        source = "scripted"
        note = ""
        if self.llm is not None and observation.get("directive"):
            directive, source, cause = await self.llm.directive(observation)
            self.directive = directive
            note = directive.note or ""
            if source == "scripted" and cause:
                note = defaults.truncate_runes(
                    note or f"holding the last directive ({cause})",
                    defaults.MAX_NOTE_RUNES,
                )
        view = micro.BoardView.from_observation(observation)
        actions = micro.compile_turn(view, seat, self.directive, baseline=self.baseline)
        reply = {
            "type": "orders",
            "turn": observation.get("turn"),
            "source": source,
            "actions": actions,
            "intent": self.directive.stance
            if self.directive.stance in defaults.INTENTS
            else "hold",
            "latencyMs": int((time.monotonic() - started) * 1000),
        }
        if note:
            reply["note"] = note
        return reply

    def on_done(self, result: dict) -> None:
        scores = result.get("scores")
        client.log(f"episode done: scores={scores} placement={result.get('placement')}")


def main() -> int:
    return client.main(HalitePolicy)


if __name__ == "__main__":
    raise SystemExit(main())
