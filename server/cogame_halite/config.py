"""Game config model for the Coworld runtime contract.

The config JSON arrives via ``COGAME_CONFIG_URI``. ``players`` and ``tokens``
are parallel arrays in seat-slot order (the coworld-ctf / cogame-moba
convention). A missing ``seed`` is derived once at parse time and recorded on
the resolved config, so it always reaches the replay header and the episode
stays re-derivable.

Only ``episode_steps`` and ``starting_halite`` vary between variants; every
other rule constant is pinned to its vendored upstream default and is rejected
here if a config tries to move it (``config_schema`` pins them too — the two
agree by ``tests/test_manifest.py``).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from . import defaults


class ConfigError(ValueError):
    """Invalid or inconsistent game config."""


@dataclass(frozen=True)
class PlayerConfig:
    name: str


#: Rule constants a variant may NOT move. Value = the pinned upstream default.
PINNED_RULE_FIELDS: dict[str, Any] = {
    "size": defaults.BOARD_SIZE,
    "spawn_cost": defaults.SPAWN_COST,
    "convert_cost": defaults.CONVERT_COST,
    "move_cost": defaults.MOVE_COST,
    "collect_rate": defaults.COLLECT_RATE,
    "regen_rate": defaults.REGEN_RATE,
    "max_cell_halite": defaults.MAX_CELL_HALITE,
}


def _positive_int(data: dict, key: str, default: int, *, lo: int, hi: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config.{key} must be an integer, got {value!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"config.{key} must be in [{lo}, {hi}], got {value}")
    return value


def _positive_number(data: dict, key: str, default: float, *, lo: float, hi: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config.{key} must be a number, got {value!r}")
    value = float(value)
    if not lo <= value <= hi:
        raise ConfigError(f"config.{key} must be in [{lo}, {hi}], got {value}")
    return value


@dataclass(frozen=True)
class GameConfig:
    players: tuple[PlayerConfig, ...]
    tokens: tuple[str, ...]
    seed: int
    episode_steps: int
    starting_halite: int
    directive_every: int
    turn_deadline_ms: int
    directive_deadline_ms: int
    directive_spacing_ms: int
    player_connect_timeout_seconds: float
    wall_clock_budget_seconds: float
    budget_guard_seconds: float
    num_agents: int = defaults.NUM_SEATS

    # Rule constants, pinned. Present so the observation and the replay carry
    # the whole resolved config and a Kaggle bot's Board(obs, config) works.
    size: int = defaults.BOARD_SIZE
    spawn_cost: int = defaults.SPAWN_COST
    convert_cost: int = defaults.CONVERT_COST
    move_cost: float = defaults.MOVE_COST
    collect_rate: float = defaults.COLLECT_RATE
    regen_rate: float = defaults.REGEN_RATE
    max_cell_halite: int = defaults.MAX_CELL_HALITE

    @property
    def num_seats(self) -> int:
        return len(self.players)

    def upstream_configuration(self) -> dict[str, Any]:
        """The ``configuration`` dict the vendored ``Board`` expects."""
        return {
            "episodeSteps": self.episode_steps,
            "startingHalite": self.starting_halite,
            "size": self.size,
            "spawnCost": self.spawn_cost,
            "convertCost": self.convert_cost,
            "moveCost": self.move_cost,
            "collectRate": self.collect_rate,
            "regenRate": self.regen_rate,
            "maxCellHalite": self.max_cell_halite,
            "randomSeed": self.seed,
            "agentTimeout": defaults.upstream_defaults()["agentTimeout"],
            "actTimeout": defaults.upstream_defaults()["actTimeout"],
            "runTimeout": defaults.upstream_defaults()["runTimeout"],
        }

    def observation_config(self) -> dict[str, Any]:
        """The rule block echoed in every ``observe`` frame (Kaggle key names)."""
        return {
            "size": self.size,
            "episodeSteps": self.episode_steps,
            "startingHalite": self.starting_halite,
            "spawnCost": self.spawn_cost,
            "convertCost": self.convert_cost,
            "moveCost": self.move_cost,
            "collectRate": self.collect_rate,
            "regenRate": self.regen_rate,
            "maxCellHalite": self.max_cell_halite,
        }

    def replay_config(self) -> dict[str, Any]:
        """Every resolved field for the replay header — **tokens excluded**."""
        doc = dict(self.observation_config())
        doc.update(
            {
                "num_agents": self.num_agents,
                "seed": self.seed,
                "directive_every": self.directive_every,
                "turn_deadline_ms": self.turn_deadline_ms,
                "directive_deadline_ms": self.directive_deadline_ms,
                "directive_spacing_ms": self.directive_spacing_ms,
                "player_connect_timeout_seconds": self.player_connect_timeout_seconds,
                "wall_clock_budget_seconds": self.wall_clock_budget_seconds,
                "budget_guard_seconds": self.budget_guard_seconds,
            }
        )
        return doc

    @classmethod
    def from_dict(cls, data: dict) -> "GameConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"config must be a JSON object, got {type(data).__name__}")

        players_raw = data.get("players")
        if not isinstance(players_raw, list) or not players_raw:
            raise ConfigError("config requires a non-empty 'players' array")
        players: list[PlayerConfig] = []
        for i, entry in enumerate(players_raw):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
            ):
                raise ConfigError(
                    f"players[{i}] must be an object with a non-empty 'name'"
                )
            players.append(PlayerConfig(name=entry["name"]))

        tokens_raw = data.get("tokens")
        if not isinstance(tokens_raw, list) or not all(
            isinstance(t, str) and t for t in tokens_raw
        ):
            raise ConfigError("config requires a 'tokens' array of non-empty strings")
        if len(tokens_raw) != len(players):
            raise ConfigError(
                f"config has {len(players)} players but {len(tokens_raw)} tokens"
            )

        num_agents = data.get("num_agents", len(players))
        if isinstance(num_agents, bool) or not isinstance(num_agents, int):
            raise ConfigError(f"config.num_agents must be an integer, got {num_agents!r}")
        if num_agents != len(players):
            raise ConfigError(
                f"config.num_agents is {num_agents} but 'players' names {len(players)} seats"
            )
        if num_agents != defaults.NUM_SEATS:
            raise ConfigError(
                f"cogame-halite is a {defaults.NUM_SEATS}-seat game; got num_agents={num_agents}"
            )

        for key, pinned in PINNED_RULE_FIELDS.items():
            if key in data and data[key] != pinned:
                raise ConfigError(
                    f"config.{key} is pinned to the upstream default {pinned!r} "
                    f"(bit-exactness); got {data[key]!r}"
                )

        seed = data.get("seed")
        if seed is None:
            seed = secrets.randbelow((1 << 31) - 1)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ConfigError(f"config.seed must be an integer, got {seed!r}")
        if not 0 <= seed < (1 << 31):
            raise ConfigError(f"config.seed must be in [0, 2^31), got {seed}")

        episode_steps = _positive_int(
            data, "episode_steps", defaults.EPISODE_STEPS, lo=2, hi=2000
        )
        starting_halite = _positive_int(
            data, "starting_halite", defaults.STARTING_HALITE, lo=1000, hi=200_000
        )
        directive_every = _positive_int(
            data, "directive_every", defaults.DEFAULT_DIRECTIVE_EVERY, lo=1, hi=400
        )
        turn_deadline_ms = _positive_int(
            data, "turn_deadline_ms", defaults.DEFAULT_TURN_DEADLINE_MS, lo=10, hi=60_000
        )
        directive_deadline_ms = _positive_int(
            data,
            "directive_deadline_ms",
            defaults.DEFAULT_DIRECTIVE_DEADLINE_MS,
            lo=100,
            hi=120_000,
        )
        directive_spacing_ms = _positive_int(
            data,
            "directive_spacing_ms",
            defaults.DEFAULT_DIRECTIVE_SPACING_MS,
            lo=0,
            hi=120_000,
        )
        connect_timeout = _positive_number(
            data,
            "player_connect_timeout_seconds",
            defaults.DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS,
            lo=1.0,
            hi=900.0,
        )
        wall_clock = _positive_number(
            data,
            "wall_clock_budget_seconds",
            defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS,
            lo=5.0,
            hi=3600.0,
        )
        budget_guard = _positive_number(
            data,
            "budget_guard_seconds",
            min(defaults.DEFAULT_BUDGET_GUARD_SECONDS, wall_clock * 0.91),
            lo=1.0,
            hi=3600.0,
        )
        if budget_guard >= wall_clock:
            raise ConfigError(
                "config.budget_guard_seconds must be below wall_clock_budget_seconds "
                f"({budget_guard} >= {wall_clock}) — the guard exists to make the "
                "hard stop unreachable"
            )

        return cls(
            players=tuple(players),
            tokens=tuple(tokens_raw),
            seed=int(seed),
            episode_steps=episode_steps,
            starting_halite=starting_halite,
            directive_every=directive_every,
            turn_deadline_ms=turn_deadline_ms,
            directive_deadline_ms=directive_deadline_ms,
            directive_spacing_ms=directive_spacing_ms,
            player_connect_timeout_seconds=connect_timeout,
            wall_clock_budget_seconds=wall_clock,
            budget_guard_seconds=budget_guard,
            num_agents=num_agents,
        )

    @classmethod
    def from_json(cls, raw: bytes | str) -> "GameConfig":
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"config is not valid JSON: {exc}") from exc
        return cls.from_dict(data)
