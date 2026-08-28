"""The port surface: board generation, turn resolution, observations, hashing.

**Nothing here re-implements a Halite rule.** ``step()`` builds the vendored
``Board(observation, configuration, actions)`` and calls its ``.next()``; the
board generator is upstream's own ``populate_board`` body reached through a
20-line adapter. The only transcription is the 12-line elimination + last-fleet
block of ``halite.py::interpreter()``, which lives here because our engine owns
seat statuses — both are named in ``vendor/PATCHES.md`` with citations, and
``tests/test_fidelity.py`` covers them.

Purity: the sim reads no clock, no environment and no disk. The global RNG
state is saved and restored around ``populate_board``, so nothing else in the
process can perturb board generation, and no other sim code calls a RNG at all.
``(seed, config, per-turn orders)`` determines the entire episode.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import defaults
from .config import GameConfig

# --------------------------------------------------------------------------
# The vendored tree. ``sim/assemble.py`` copies vendor/upstream + sim/shim into
# build/khalite and puts it on sys.path; from there the imports below resolve
# to the byte-pristine upstream modules.
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sim.assemble import assembled_root  # noqa: E402

assembled_root()

from kaggle_environments.envs.halite.halite import (  # noqa: E402
    populate_board as _upstream_populate_board,
    specification as UPSTREAM_SPEC,
)
from kaggle_environments.envs.halite.helpers import (  # noqa: E402
    Board,
    Configuration,
    ShipAction,
    ShipyardAction,
)
from kaggle_environments.helpers import Point  # noqa: E402

__all__ = [
    "Board",
    "HaliteGuardError",
    "HaliteSim",
    "Point",
    "SeatStats",
    "ShipAction",
    "ShipyardAction",
    "TurnResult",
    "UPSTREAM_SPEC",
    "state_hash_of",
]

#: Guard trip -> reason "fault", artifacts still written.
MAX_SHIPS = 5000


class HaliteGuardError(RuntimeError):
    """A sim invariant broke: negative bank/cell, oversized order map, an
    unknown action value reaching the sim, an escaping upstream ``assert``."""


# --------------------------------------------------------------------------
# Duck types for the upstream populate_board adapter (vendor/PATCHES.md §1).
# populate_board(state, env) wants: env.configuration.{size, startingHalite,
# randomSeed}, state[0].observation.{step, halite, players}, state[0].reward.
# --------------------------------------------------------------------------


class _Attr(dict):
    """A dict that is also attribute-addressable (upstream's ``Struct``)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


@dataclass
class SeatStats:
    mined: int = 0
    stolen: int = 0
    collisions_won: int = 0
    collisions_lost: int = 0
    ships_built: int = 0
    yards_built: int = 0
    deposited: int = 0


@dataclass
class TurnResult:
    turn: int
    events: list[dict] = field(default_factory=list)
    eliminated_this_turn: list[int] = field(default_factory=list)
    last_fleet: bool = False


def _fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for byte in data:
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def state_hash_of(
    turn: int,
    halite: Iterable[float],
    players: list,
    eliminated: list[int | None],
) -> str:
    """FNV-1a 64 over a canonical encoding, as 16 lowercase hex digits.

    Canonical encoding: ``turn``, then each cell's halite formatted ``%.3f`` in
    index order, then per seat the ``bank``, the sorted ``(yard_id, pos)``
    pairs, the sorted ``(ship_id, pos, cargo)`` triples, then the elimination
    turns. Recorded per turn in the replay; the re-derivation test asserts
    every one of them.
    """
    parts: list[str] = [f"t{turn}"]
    parts.append("|".join(f"{float(h):.3f}" for h in halite))
    for bank, yards, ships in players:
        parts.append(f"b{int(bank)}")
        parts.append(",".join(f"{k}@{int(v)}" for k, v in sorted(yards.items())))
        parts.append(
            ",".join(
                f"{k}@{int(v[0])}:{int(v[1])}" for k, v in sorted(ships.items())
            )
        )
    parts.append(",".join("-" if e is None else str(e) for e in eliminated))
    canonical = "\x1f".join(parts).encode("utf-8")
    return f"{_fnv1a64(canonical):016x}"


class HaliteSim:
    """One Halite episode. ``reset()`` then ``step(orders)`` ``episode_steps-1``
    times."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self.size = config.size
        self.num_seats = config.num_seats
        self.turn = 0
        self.halite: list[float] = []
        #: ``[[bank, {yardId: pos}, {shipId: [pos, cargo]}], ...]`` — Kaggle's
        #: observation shape, key for key, insertion order preserved.
        self.players: list[list] = []
        self.eliminated: list[int | None] = [None] * self.num_seats
        self.stats: list[SeatStats] = [SeatStats() for _ in range(self.num_seats)]
        self.last_fleet = False
        self._configuration = Configuration(config.upstream_configuration())

    # ---------------------------------------------------------------- reset

    def reset(self) -> None:
        """Generate the board with upstream's own ``populate_board``.

        vendor/PATCHES.md §1: the adapter builds the ``state``/``env`` duck
        types; the function body is upstream's, imported from the assembled
        tree. The global RNG state is saved and restored around the call so
        nothing else in the process can perturb board generation.
        """
        observation = _Attr(step=0, halite=[], players=[])
        state = [_Attr(observation=observation, reward=defaults.STARTING_BANK)]
        for _ in range(self.num_seats - 1):
            state.append(_Attr(observation=_Attr(), reward=defaults.STARTING_BANK))
        env = _Attr(configuration=_Attr(**self.config.upstream_configuration()))

        py_state = random.getstate()
        try:
            import numpy as np

            np_state = np.random.get_state()
        except Exception:  # pragma: no cover - numpy is a hard dependency
            np_state = None
        try:
            _upstream_populate_board(state, env)
        finally:
            random.setstate(py_state)
            if np_state is not None:
                import numpy as np

                np.random.set_state(np_state)

        self.turn = 0
        self.halite = [float(h) for h in observation.halite]
        self.players = [
            [int(bank), dict(yards), {k: list(v) for k, v in ships.items()}]
            for bank, yards, ships in observation.players
        ]
        self.eliminated = [None] * self.num_seats
        self.stats = [SeatStats() for _ in range(self.num_seats)]
        self.last_fleet = False
        self._guard()

    # ----------------------------------------------------------------- step

    def _observation_dict(self, player: int = 0) -> dict:
        return {
            "halite": list(self.halite),
            "players": [
                [bank, dict(yards), {k: list(v) for k, v in ships.items()}]
                for bank, yards, ships in self.players
            ],
            "player": player,
            "step": self.turn,
            "remainingOverageTime": defaults.upstream_defaults()["remainingOverageTime"]
            if "remainingOverageTime" in defaults.upstream_defaults()
            else defaults.upstream_spec()["observation"]["remainingOverageTime"],
        }

    def step(self, orders: list[dict[str, str]]) -> TurnResult:
        """Apply one turn. ``orders[p]`` is seat p's accepted action map."""
        if len(orders) != self.num_seats:
            raise HaliteGuardError(
                f"step() wants {self.num_seats} order maps, got {len(orders)}"
            )
        for seat, mapping in enumerate(orders):
            if len(mapping) > defaults.MAX_ACTIONS_PER_TURN:
                raise HaliteGuardError(
                    f"seat {seat} order map has {len(mapping)} entries "
                    f"(cap {defaults.MAX_ACTIONS_PER_TURN})"
                )
            for key, value in mapping.items():
                if value not in defaults.ALL_ACTIONS:
                    raise HaliteGuardError(
                        f"seat {seat} order {key!r} has unknown action {value!r}"
                    )

        # Eliminated seats can never act again.
        effective = [
            {} if self.eliminated[s] is not None else dict(orders[s])
            for s in range(self.num_seats)
        ]

        before = self._snapshot()
        try:
            board = Board(self._observation_dict(), self._configuration, effective)
            nxt = board.next()
        except AssertionError as exc:
            raise HaliteGuardError(f"upstream assertion failed: {exc}") from exc
        obs = nxt.observation

        self.turn = int(obs["step"])
        self.halite = [float(h) for h in obs["halite"]]
        self.players = [
            [int(bank), dict(yards), {k: list(v) for k, v in ships.items()}]
            for bank, yards, ships in obs["players"]
        ]

        events = self._derive_events(before, effective)
        result = TurnResult(turn=self.turn, events=events)
        self._eliminate(result)
        self._guard()
        return result

    # ---------------------------------------------------- elimination block

    def _eliminate(self, result: TurnResult) -> None:
        """Transcription of ``halite.py::interpreter()`` lines 194-209.

        Upstream::

            for index, agent in enumerate(state):
                player_halite, shipyards, ships = obs.players[index]
                if agent.status == "ACTIVE" and len(ships) == 0 and (
                        len(shipyards) == 0 or player_halite < config.spawnCost):
                    agent.status = "DONE"
                    agent.reward = board.step - board.configuration.episode_steps - 1
            if len(state) > 1 and sum(1 for a in state if a.status == "ACTIVE") < 2:
                for agent in state:
                    if agent.status == "ACTIVE":
                        agent.status = "DONE"

        The constants come from the vendored ``Configuration``, never re-typed.

        **An eliminated seat keeps its assets.** Upstream clears
        ``obs.players[index]`` only for an agent whose status is neither
        ACTIVE nor DONE (line 201-202 above: an INVALID/TIMEOUT/ERROR agent,
        a status this engine never produces — a seat that stops answering is
        substituted for, never invalidated). A **DONE** agent — which is what
        elimination makes — keeps its shipyards in ``obs.players``, so an
        unfunded yard stays on the board as a razing hazard and a
        mining-suppressing cell for the seats still playing. Clearing it here
        would be fixing an upstream quirk, which ``docs/PORTING.md`` forbids.
        """
        spawn_cost = self._configuration.spawn_cost
        for seat in range(self.num_seats):
            if self.eliminated[seat] is not None:
                continue
            bank, yards, ships = self.players[seat]
            if len(ships) == 0 and (len(yards) == 0 or bank < spawn_cost):
                self.eliminated[seat] = self.turn
                result.eliminated_this_turn.append(seat)
                result.events.append({"k": "eliminate", "seat": seat, "turn": self.turn})
        if self.num_seats > 1:
            active = sum(1 for e in self.eliminated if e is None)
            if active < 2:
                self.last_fleet = True
                result.last_fleet = True

    # -------------------------------------------------------------- helpers

    def _snapshot(self) -> dict:
        cells = {}
        ships: dict[str, tuple[int, int, int]] = {}
        yards: dict[str, tuple[int, int]] = {}
        for seat, (bank, syards, sships) in enumerate(self.players):
            cells[seat] = bank
            for sid, (pos, cargo) in sships.items():
                ships[sid] = (seat, int(pos), int(cargo))
            for yid, pos in syards.items():
                yards[yid] = (seat, int(pos))
        return {
            "banks": list(cells.values()),
            "ships": ships,
            "yards": yards,
            "halite": list(self.halite),
        }

    def _derive_events(self, before: dict, orders: list[dict[str, str]]) -> list[dict]:
        """The replay's event vocabulary, derived from before/after state.

        Every kind in the design note's event table except ``note``,
        ``fallback``, ``strike``, ``budget_guard`` and ``stop`` (which the
        engine owns because they are wall-clock / transport facts, not sim
        facts).
        """
        events: list[dict] = []
        after = self._snapshot()
        old_ships, new_ships = before["ships"], after["ships"]
        old_yards, new_yards = before["yards"], after["yards"]

        # spawn / convert — new asset ids are minted f"{turn}-{n}".
        for sid, (seat, pos, _cargo) in new_ships.items():
            if sid not in old_ships:
                events.append({"k": "spawn", "seat": seat, "ship": sid, "pos": pos})
                self.stats[seat].ships_built += 1
        converted: dict[str, str] = {}
        for yid, (seat, pos) in new_yards.items():
            if yid in old_yards:
                continue
            source = ""
            for sid, action in orders[seat].items():
                if action == "CONVERT" and sid in old_ships and old_ships[sid][1] == pos:
                    source = sid
                    break
            converted[yid] = source
            events.append(
                {"k": "convert", "seat": seat, "ship": source, "yard": yid, "pos": pos}
            )
            self.stats[seat].yards_built += 1

        # ship-to-ship collisions: every cell that lost more than it kept.
        lost_by_pos: dict[int, list[dict]] = {}
        for sid, (seat, pos, cargo) in old_ships.items():
            if sid in new_ships or sid in converted.values():
                continue
            moved = orders[seat].get(sid)
            end = _translate(pos, moved, self.size)
            lost_by_pos.setdefault(end, []).append(
                {"seat": seat, "ship": sid, "cargo": cargo}
            )
        survivor_by_pos: dict[int, dict] = {}
        for sid, (seat, pos, _cargo) in new_ships.items():
            survivor_by_pos[pos] = {"seat": seat, "ship": sid}
        razed_positions = {
            pos for yid, (_seat, pos) in old_yards.items() if yid not in new_yards
        }
        for pos, lost in sorted(lost_by_pos.items()):
            survivor = survivor_by_pos.get(pos)
            yard_seat = None
            for yid, (seat, ypos) in old_yards.items():
                if ypos == pos and yid not in new_yards:
                    yard_seat = (seat, yid)
                    break
            if yard_seat is not None and len(lost) == 1:
                seat, yid = yard_seat
                events.append(
                    {
                        "k": "yardraze",
                        "pos": pos,
                        "yardSeat": seat,
                        "yard": yid,
                        "shipSeat": lost[0]["seat"],
                        "ship": lost[0]["ship"],
                    }
                )
                self.stats[lost[0]["seat"]].collisions_lost += 1
                continue
            stolen = sum(entry["cargo"] for entry in lost) if survivor else 0
            events.append(
                {
                    "k": "collide",
                    "pos": pos,
                    "survivor": survivor,
                    "lost": lost,
                    "stolen": stolen,
                }
            )
            if survivor:
                self.stats[survivor["seat"]].collisions_won += 1
                self.stats[survivor["seat"]].stolen += stolen
            for entry in lost:
                self.stats[entry["seat"]].collisions_lost += 1
            if pos in razed_positions and survivor is None:
                pass

        # deposit — a surviving ship on its own yard banked its cargo.
        for sid, (seat, pos, cargo) in new_ships.items():
            if cargo != 0:
                continue
            was = old_ships.get(sid)
            carried = was[2] if was else 0
            if sid in converted.values():
                continue
            yard_here = None
            for yid, (yseat, ypos) in new_yards.items():
                if ypos == pos and yseat == seat:
                    yard_here = yid
                    break
            if yard_here is None or carried <= 0:
                continue
            gained = sum(entry["cargo"] for entry in lost_by_pos.get(pos, []))
            amount = carried + gained
            events.append(
                {
                    "k": "deposit",
                    "seat": seat,
                    "ship": sid,
                    "yard": yard_here,
                    "pos": pos,
                    "amount": amount,
                }
            )
            self.stats[seat].deposited += amount

        # mine — a surviving ship whose cargo grew on a cell that shrank.
        for sid, (seat, pos, cargo) in new_ships.items():
            was = old_ships.get(sid)
            if was is None:
                continue
            old_cell = before["halite"][pos]
            new_cell = after["halite"][pos]
            delta = int(old_cell * self.config.collect_rate)
            if delta > 0 and abs((old_cell - delta) - new_cell) < 1e-6 and pos == was[1]:
                events.append(
                    {"k": "mine", "seat": seat, "ship": sid, "pos": pos, "amount": delta}
                )
                self.stats[seat].mined += delta
        return events

    def _guard(self) -> None:
        total_ships = 0
        for seat, (bank, yards, ships) in enumerate(self.players):
            if bank < 0:
                raise HaliteGuardError(f"seat {seat} bank went negative: {bank}")
            total_ships += len(ships)
            for sid, (_pos, cargo) in ships.items():
                if cargo < 0:
                    raise HaliteGuardError(f"ship {sid} cargo went negative: {cargo}")
        if total_ships > MAX_SHIPS:
            raise HaliteGuardError(f"ship count {total_ships} exceeds {MAX_SHIPS}")
        for index, cell in enumerate(self.halite):
            if cell < 0:
                raise HaliteGuardError(f"cell {index} halite went negative: {cell}")

    # -------------------------------------------------------- port surface

    def observation(self, seat: int) -> dict:
        """The Kaggle observation object for ``seat`` (key for key)."""
        return self._observation_dict(player=seat)

    def ascii_board(self) -> str:
        """The vendored ``Board.__str__`` — never a re-write."""
        return str(Board(self._observation_dict(), self._configuration))

    def state_hash(self) -> str:
        return state_hash_of(self.turn, self.halite, self.players, self.eliminated)

    def banks(self) -> list[int]:
        return [int(p[0]) for p in self.players]

    def ship_counts(self) -> list[int]:
        return [len(p[2]) for p in self.players]

    def yard_counts(self) -> list[int]:
        return [len(p[1]) for p in self.players]

    def cargo_afloat(self) -> list[int]:
        return [sum(int(v[1]) for v in p[2].values()) for p in self.players]

    def owns(self, seat: int, asset_id: str) -> bool:
        _bank, yards, ships = self.players[seat]
        return asset_id in ships or asset_id in yards

    def is_ship(self, seat: int, asset_id: str) -> bool:
        return asset_id in self.players[seat][2]

    def is_shipyard(self, seat: int, asset_id: str) -> bool:
        return asset_id in self.players[seat][1]

    def replay_turn_state(self) -> dict:
        """The per-turn state block of the replay document (halite as ints)."""
        return {
            "halite": [int(round(h)) for h in self.halite],
            "players": [
                [int(bank), dict(yards), {k: [int(v[0]), int(v[1])] for k, v in ships.items()}]
                for bank, yards, ships in self.players
            ],
        }


def _translate(pos: int, action: str | None, size: int) -> int:
    """Where a ship that was at ``pos`` and took ``action`` ended up.

    Mirrors ``Point.translate`` through ``Point.from_index``/``to_index``, i.e.
    the vendored geometry, so the derived events agree with the resolution.
    """
    if action not in ("NORTH", "SOUTH", "EAST", "WEST"):
        return pos
    point = Point.from_index(pos, size)
    offset = ShipAction[action].to_point()
    return point.translate(offset, size).to_index(size)
