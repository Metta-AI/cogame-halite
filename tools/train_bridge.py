#!/usr/bin/env python3
"""Persistent numeric Halite decisions over the production simulator."""

import json
import sys
import zlib
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

from cogame_halite import defaults, micro, results  # noqa: E402
from cogame_halite.config import GameConfig  # noqa: E402
from cogame_halite.engine import Engine  # noqa: E402
from players import llm  # noqa: E402

VARIANTS = ("standard", "sprint", "richfields")
SPAWN_UNTIL = (0, 50, 100, 150, 200, 250, 300, 350, 400)
MINE_FLOOR = tuple(range(0, 501, 50))
RETURN_AT = tuple(range(50, 1501, 50))
VALUE_COUNT = 1788


class Bridge:
    def __init__(self, manifest: Path, variant: str) -> None:
        assert variant in VARIANTS
        self.variant = variant
        document = json.loads(manifest.read_text())
        self.variant_config = next(v["game_config"] for v in document["variants"] if v["id"] == variant)
        self.engine = None
        self.directives = []
        self.previous = []
        self.actions = []
        self.cursor = 0
        self.decision_id = 0

    @property
    def sim(self):
        return self.engine.sim

    def heads(self) -> list[dict]:
        return [
            {"name": "stance", "choices": list(llm.STANCES)},
            {"name": "spawnUntil", "choices": list(SPAWN_UNTIL)},
            {"name": "yards", "choices": [1, 2, 3, 4]},
            {"name": "mineFloor", "choices": list(MINE_FLOOR)},
            {"name": "returnAt", "choices": list(RETURN_AT)},
            {"name": "focus", "choices": list(llm.FOCI)},
            {"name": "avoid", "choices": ["none"] + [defaults.ALIASES[s] for s in range(4) if s != self.cursor]},
        ]

    def current(self) -> dict:
        seat = self.cursor
        observation = self.engine._observation(seat, self.sim.turn, True, self.engine.config.directive_deadline_ms)
        properties = {head["name"]: {"enum": head["choices"]} for head in self.heads()}
        return {
            "kind": "decision", "game": "halite", "decision_id": self.decision_id,
            "seat": seat, "engine_seat": seat, "turn": self.sim.turn,
            "semantic_view": observation, "inbox": [],
            "messages": [
                {"role": "system", "content": llm.SYSTEM_PROMPT},
                {"role": "user", "content": llm.build_prompt(observation, "", self.previous[seat])},
            ],
            "speech_messages": [],
            "action_schema": {"type": "object", "properties": properties,
                              "required": [head["name"] for head in self.heads()]},
            "typed_question": None,
        }

    def encode(self) -> dict:
        sim = self.sim
        values = [int(self.variant == name) for name in VARIANTS]
        values.extend(int(self.cursor == seat) for seat in range(4))
        values.append(sim.turn / sim.config.episode_steps)
        for bank, yards, ships in sim.players:
            values.extend((bank / 100000, len(yards) / 20, len(ships) / 24,
                           sum(ship[1] for ship in ships.values()) / 24000))
        ship_at = {}
        yard_at = {}
        for seat, (_, yards, ships) in enumerate(sim.players):
            for position in yards.values():
                yard_at[position] = seat + 1
            for position, cargo in ships.values():
                ship_at[position] = (seat + 1, cargo)
        for cell, halite in enumerate(sim.halite):
            owner, cargo = ship_at.get(cell, (0, 0))
            values.extend((halite / 500, owner / 4, cargo / 10000, yard_at.get(cell, 0) / 4))
        assert len(values) == VALUE_COUNT
        return {"decision_id": self.decision_id, "values": values,
                "action_heads": self.heads()}

    def reset(self, request: dict) -> dict:
        assert request["players"] == 4
        seed = zlib.crc32(request["seed"].encode()) & 0x7FFFFFFF
        config = GameConfig.from_dict({**self.variant_config, "seed": seed,
                                       "tokens": [f"training-{seat}" for seat in range(4)]})
        self.engine = Engine(config, [None] * 4)
        self.sim.reset()
        self.directives = [micro.TIDEWALKER] * 4
        self.previous = [None] * 4
        self.actions = [None] * 4
        self.cursor = 0
        self.decision_id = 0
        return self.current()

    def teacher(self) -> dict:
        action = micro.TIDEWALKER.as_dict()
        action.pop("note")
        action["avoid"] = "none"
        return {"response": json.dumps(action, separators=(",", ":"))}

    def step(self, request: dict) -> dict:
        assert request["decision_id"] == self.decision_id
        action = json.loads(request["response"])
        for head in self.heads():
            name = head["name"]
            assert action[name] in head["choices"], f"action is masked: {name}"
        self.actions[self.cursor] = action
        self.cursor += 1
        self.decision_id += 1
        if self.cursor == 4:
            for seat, choice in enumerate(self.actions):
                self.previous[seat] = self.directives[seat]
                raw = {**choice, "avoid": None if choice["avoid"] == "none" else choice["avoid"]}
                directive = llm.repair(raw, self.directives[seat], list(defaults.ALIASES),
                                       seat, self.engine.config.episode_steps)
                assert replace(directive, note="") == directive
                self.directives[seat] = directive
            last_fleet = False
            while self.sim.turn < self.engine.config.episode_steps - 1:
                view = micro.BoardView.from_sim(self.sim)
                orders = [micro.compile_turn(view, seat, self.directives[seat], baseline="tidewalker")
                          for seat in range(4)]
                last_fleet = self.sim.step(orders).last_fleet
                if last_fleet or self.sim.turn % self.engine.config.directive_every == 0:
                    break
            self.cursor = 0
            if last_fleet or self.sim.turn >= self.engine.config.episode_steps - 1:
                scores = [results.score_of(int(self.sim.players[seat][0]), self.sim.eliminated[seat],
                                           self.engine.config.episode_steps) for seat in range(4)]
                assets = [len(player[1]) + len(player[2]) for player in self.sim.players]
                mined = [stat.mined for stat in self.sim.stats]
                _, ranking = results.placement_and_ranking(scores, assets, mined)
                normalized = {str(seat): (3 - ranking.index(seat)) / 3 for seat in range(4)}
                observation = {"kind": "terminal", "scores": normalized,
                               "utilities": {seat: 2 * score - 1 for seat, score in normalized.items()}}
            else:
                observation = self.current()
        else:
            observation = self.current()
        return {"kind": "accepted", "action": action, "observation": observation}


if __name__ == "__main__":
    assert len(sys.argv) == 3, "usage: train_bridge.py MANIFEST VARIANT"
    bridge = Bridge(Path(sys.argv[1]).resolve(), sys.argv[2])
    for line in sys.stdin:
        request = json.loads(line)
        response = {
            "reset": bridge.reset,
            "encode": lambda _request: bridge.encode(),
            "teacher": lambda _request: bridge.teacher(),
            "step": bridge.step,
        }[request["kind"]](request)
        print(json.dumps(response, separators=(",", ":")), flush=True)
