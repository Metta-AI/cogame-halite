"""``tidewalker`` and ``corsair`` — the scripted baselines *and* the executor of
every LLM directive.

One pure function, :func:`compile_turn`, imported by
``players/halite_player.py`` **and** by the engine's server-side fallback path,
so the two can never drift. It is the Kaggle-proven trio — mine-richest-nearby,
return-when-full, avoid-heavier-collisions — made deterministic.

Bounded orders are a tested property, not a hope (``tests/test_micro.py``): at
most one action per owned asset, only assets the seat owns, only enum values,
at most 256 entries, never a ``SPAWN`` the bank cannot pay, never a ``CONVERT``
onto a cell holding a shipyard.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import defaults

#: Tie-break for any move choice, after (lower unsafe, higher target halite).
DIRECTION_ORDER = ("NORTH", "EAST", "SOUTH", "WEST")

#: Board-index deltas for a direction, computed per size in :func:`_neighbours`.
_STANCES = ("expand", "mine", "raid", "defend")
_FOCI = ("NW", "NE", "SW", "SE", "CENTER")

MAX_SHIPS = 24
PATCH_RADIUS = 6


@dataclass(frozen=True)
class Directive:
    #: The field defaults ARE `TIDEWALKER` and the turn-0 directive every LLM
    #: seat starts from. `mineFloor`, `returnAt` and `spawnUntil` are the
    #: winners of the grid sweep in `tools/tune/grid_search.py`, confirmed on
    #: fresh seeds; the run that chose them is
    #: `docs/tuning/2026-08-28-micro-grid.md`, and `tests/test_tuning.py`
    #: asserts these values are the ones it selected.
    stance: str = "mine"
    spawnUntil: int = 200
    yards: int = 2
    mineFloor: int = 200
    returnAt: int = 300
    focus: str = "CENTER"
    avoid: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "spawnUntil": self.spawnUntil,
            "yards": self.yards,
            "mineFloor": self.mineFloor,
            "returnAt": self.returnAt,
            "focus": self.focus,
            "avoid": self.avoid,
            "note": self.note,
        }


#: The turn-0 defaults every seat starts from (the design note's table).
TIDEWALKER = Directive()

#: ``corsair`` — the same function with a raider's constants and one extra
#: rule: hunting is always on and it chases a heavy enemy up to distance 4 even
#: at ``stance != "raid"``. Its constants are the grid sweep's winner for this
#: baseline (`docs/tuning/2026-08-28-micro-grid.md`); it keeps spawning 100
#: turns longer than tidewalker, which is the axis the sweep separates them on.
CORSAIR = Directive(stance="raid", spawnUntil=300, yards=2, mineFloor=200, returnAt=300)

BASELINE_DIRECTIVES: dict[str, Directive] = {
    "tidewalker": TIDEWALKER,
    "corsair": CORSAIR,
}


def baseline_directive(name: str) -> Directive:
    """The directive a scripted baseline plays. Unknown name -> tidewalker."""
    return BASELINE_DIRECTIVES.get(name, TIDEWALKER)


# --------------------------------------------------------------------------
# Torus geometry. Board index = (size - y - 1) * size + x, so index 0 is the
# top-left cell on screen; NORTH is index - size.
# --------------------------------------------------------------------------


def xy(pos: int, size: int) -> tuple[int, int]:
    y, x = divmod(pos, size)
    return x, size - 1 - y


def to_index(x: int, y: int, size: int) -> int:
    return (size - (y % size) - 1) * size + (x % size)


def step_index(pos: int, direction: str, size: int) -> int:
    x, y = xy(pos, size)
    if direction == "NORTH":
        y += 1
    elif direction == "SOUTH":
        y -= 1
    elif direction == "EAST":
        x += 1
    elif direction == "WEST":
        x -= 1
    return to_index(x, y, size)


def dist(a: int, b: int, size: int) -> int:
    ax, ay = xy(a, size)
    bx, by = xy(b, size)
    dx = abs(ax - bx)
    dy = abs(ay - by)
    return min(dx, size - dx) + min(dy, size - dy)


def _neighbours(pos: int, size: int) -> list[int]:
    return [step_index(pos, d, size) for d in DIRECTION_ORDER]


def _quadrant_centre(focus: str, size: int) -> int:
    q = size // 4
    three = (3 * size) // 4
    table = {
        "NW": (q, three),
        "NE": (three, three),
        "SW": (q, q),
        "SE": (three, q),
        "CENTER": (size // 2, size // 2),
    }
    x, y = table.get(focus, table["CENTER"])
    return to_index(x, y, size)


# --------------------------------------------------------------------------
# State view. `state` is anything with `.size`, `.halite`, `.players`,
# `.turn`, `.config.episode_steps` — HaliteSim satisfies it, and so does the
# lightweight view players build from an `observe` frame.
# --------------------------------------------------------------------------


@dataclass
class BoardView:
    size: int
    turn: int
    max_turns: int
    halite: list[float]
    players: list[list]

    @classmethod
    def from_observation(cls, obs: dict) -> "BoardView":
        cfg = obs.get("config") or {}
        return cls(
            size=int(cfg.get("size", defaults.BOARD_SIZE)),
            turn=int(obs.get("turn", 0)),
            max_turns=int(obs.get("maxTurns", cfg.get("episodeSteps", defaults.EPISODE_STEPS))),
            halite=[float(h) for h in obs.get("halite", [])],
            players=[
                [int(p[0]), dict(p[1]), {k: [int(v[0]), int(v[1])] for k, v in p[2].items()}]
                for p in obs.get("players", [])
            ],
        )

    @classmethod
    def from_sim(cls, sim) -> "BoardView":
        return cls(
            size=sim.size,
            turn=sim.turn,
            max_turns=sim.config.episode_steps,
            halite=list(sim.halite),
            players=[
                [int(p[0]), dict(p[1]), {k: [int(v[0]), int(v[1])] for k, v in p[2].items()}]
                for p in sim.players
            ],
        )


def compile_turn(
    state: BoardView, seat: int, directive: Directive, *, baseline: str = "tidewalker"
) -> dict[str, str]:
    """Compile one turn of orders for ``seat``. Pure and deterministic."""
    size = state.size
    if seat >= len(state.players):
        return {}
    bank, yards, ships = state.players[seat]
    orders: dict[str, str] = {}

    always_hunt = baseline == "corsair"
    hunt_range = 4 if always_hunt else 3

    # Enemy ships by cell, and the lightest enemy cargo touching each cell.
    enemy_threat: dict[int, int] = {}
    enemy_ships: list[tuple[int, int, int]] = []  # (pos, cargo, seat)
    for other, (_b, _y, oships) in enumerate(state.players):
        if other == seat:
            continue
        for pos, cargo in oships.values():
            enemy_ships.append((int(pos), int(cargo), other))
            for cell in (int(pos), *_neighbours(int(pos), size)):
                cur = enemy_threat.get(cell)
                if cur is None or int(cargo) < cur:
                    enemy_threat[cell] = int(cargo)

    my_yard_cells = {int(p) for p in yards.values()}
    all_yard_cells: set[int] = set()
    for _b, oyards, _s in state.players:
        all_yard_cells |= {int(p) for p in oyards.values()}

    def unsafe(cell: int, my_cargo: int) -> bool:
        if cell in my_yard_cells:
            return False
        threat = enemy_threat.get(cell)
        return threat is not None and threat <= my_cargo

    claimed: set[int] = set()

    def cell_halite(cell: int) -> float:
        return state.halite[cell] if 0 <= cell < len(state.halite) else 0.0

    # ---- 1. shipyard-loss guard -------------------------------------------
    ship_items = sorted(ships.items(), key=lambda kv: _uid_key(kv[0]))
    converted: set[str] = set()
    if not yards:
        best = None
        for sid, (pos, cargo) in ship_items:
            if int(pos) in all_yard_cells:
                continue
            if int(cargo) + bank >= defaults.CONVERT_COST and (
                best is None or int(cargo) > best[1]
            ):
                best = (sid, int(cargo))
        if best is not None:
            orders[best[0]] = "CONVERT"
            converted.add(best[0])
    # ---- 2. second yard ----------------------------------------------------
    elif len(ships) >= 8 and len(yards) < directive.yards and bank >= 1500:
        best = None
        for sid, (pos, cargo) in ship_items:
            pos = int(pos)
            if pos in all_yard_cells:
                continue
            if int(cargo) + bank < defaults.CONVERT_COST:
                continue
            far = min(dist(pos, y, size) for y in my_yard_cells)
            if far >= 5 and (best is None or far > best[1]):
                best = (sid, far)
        if best is not None:
            orders[best[0]] = "CONVERT"
            converted.add(best[0])

    # ---- 3. ships, ascending uid -------------------------------------------
    for sid, (raw_pos, raw_cargo) in ship_items:
        if sid in converted:
            continue
        pos, cargo = int(raw_pos), int(raw_cargo)
        home = _nearest(pos, my_yard_cells, size)
        home_dist = dist(pos, home, size) if home is not None else 10**6

        come_home = home is not None and (
            cargo >= directive.returnAt
            or state.turn + home_dist + 2 >= state.max_turns
            or (unsafe(pos, cargo) and home_dist < 3)
        )
        if come_home:
            if pos == home:
                claimed.add(pos)
                continue
            move = _step_home(pos, home, size, cargo, unsafe, claimed, cell_halite)
            if move is not None:
                orders[sid] = move
                claimed.add(step_index(pos, move, size))
            else:
                claimed.add(pos)
            continue

        # b. mine
        if (
            pos not in all_yard_cells
            and cell_halite(pos) >= directive.mineFloor
            and not unsafe(pos, cargo)
        ):
            claimed.add(pos)
            continue

        # c. hunt
        if always_hunt or directive.stance == "raid":
            target = None
            if cargo <= 100:
                for epos, ecargo, _eseat in enemy_ships:
                    if ecargo < cargo + 200:
                        continue
                    d = dist(pos, epos, size)
                    if d <= hunt_range and (target is None or d < target[1]):
                        target = (epos, d)
            if target is not None:
                move = _step_toward(pos, target[0], size, cargo, unsafe, claimed, cell_halite)
                if move is not None:
                    orders[sid] = move
                    claimed.add(step_index(pos, move, size))
                    continue

        # d. best patch
        move = _step_to_patch(
            pos, cargo, size, directive, unsafe, claimed, cell_halite, state
        )
        if move is not None:
            orders[sid] = move
            claimed.add(step_index(pos, move, size))
        else:
            claimed.add(pos)

    # ---- 4. shipyards, ascending uid ---------------------------------------
    spawns = 0
    occupied = {int(p) for p, _c in ships.values()}
    for yid, ypos in sorted(yards.items(), key=lambda kv: _uid_key(kv[0])):
        ypos = int(ypos)
        if state.turn > directive.spawnUntil:
            break
        if bank - defaults.SPAWN_COST * spawns < defaults.SPAWN_COST:
            break
        if len(ships) + spawns >= MAX_SHIPS:
            break
        if ypos in occupied or ypos in claimed:
            continue
        orders[yid] = "SPAWN"
        spawns += 1

    if len(orders) > defaults.MAX_ACTIONS_PER_TURN:
        keep = sorted(orders, key=_uid_key)[: defaults.MAX_ACTIONS_PER_TURN]
        orders = {k: orders[k] for k in keep}
    return orders


def _uid_key(uid: str) -> tuple[int, int, str]:
    """Sort key for a Halite uid ``f"{turn}-{n}"`` — numeric, not lexical."""
    turn, _, n = uid.partition("-")
    try:
        return (int(turn), int(n), uid)
    except ValueError:
        return (10**9, 10**9, uid)


def _nearest(pos: int, cells: set[int], size: int) -> int | None:
    if not cells:
        return None
    return min(sorted(cells), key=lambda c: (dist(pos, c, size), c))


def _rank(cell: int, cargo: int, unsafe, cell_halite) -> tuple[int, float, int]:
    """Tie-break: lower unsafe, then higher target-cell halite, then order."""
    return (1 if unsafe(cell, cargo) else 0, -cell_halite(cell), 0)


def _choose(
    pos: int, dirs: list[str], size: int, cargo: int, unsafe, claimed, cell_halite
) -> str | None:
    best: tuple[tuple[int, float, int], str] | None = None
    for order, direction in enumerate(dirs):
        cell = step_index(pos, direction, size)
        if cell in claimed:
            continue
        key = (1 if unsafe(cell, cargo) else 0, -cell_halite(cell), order)
        if best is None or key < best[0]:
            best = (key, direction)
    return best[1] if best else None


def _shortest_dirs(pos: int, target: int, size: int) -> list[str]:
    here = dist(pos, target, size)
    return [
        d
        for d in DIRECTION_ORDER
        if dist(step_index(pos, d, size), target, size) < here
    ]


def _step_home(pos, home, size, cargo, unsafe, claimed, cell_halite) -> str | None:
    dirs = _shortest_dirs(pos, home, size)
    move = _choose(pos, dirs, size, cargo, unsafe, claimed, cell_halite)
    if move is not None and not unsafe(step_index(pos, move, size), cargo):
        return move
    # Every shortest step is unsafe: take the safest step that does not
    # increase the distance by more than 1. HOLDING increases it by nothing, so
    # it is inside that set and it is what a loaded ship does when every step
    # would put it beside something lighter (tests/test_micro.py's safety
    # property).
    here = dist(pos, home, size)
    widened = [
        d
        for d in DIRECTION_ORDER
        if dist(step_index(pos, d, size), home, size) <= here + 1
    ]
    safest = _choose(pos, widened, size, cargo, unsafe, claimed, cell_halite)
    if safest is not None and not unsafe(step_index(pos, safest, size), cargo):
        return safest
    if pos not in claimed and not unsafe(pos, cargo):
        return None
    return safest if safest is not None else move


def _step_toward(pos, target, size, cargo, unsafe, claimed, cell_halite) -> str | None:
    dirs = [
        d
        for d in _shortest_dirs(pos, target, size)
        if not unsafe(step_index(pos, d, size), cargo)
    ]
    return _choose(pos, dirs, size, cargo, unsafe, claimed, cell_halite)


def _step_to_patch(
    pos, cargo, size, directive, unsafe, claimed, cell_halite, state
) -> str | None:
    focus_cell = _quadrant_centre(directive.focus, size)
    best_cell = None
    best_score = 0.0
    px, py = xy(pos, size)
    for dy in range(-PATCH_RADIUS, PATCH_RADIUS + 1):
        span = PATCH_RADIUS - abs(dy)
        for dx in range(-span, span + 1):
            cell = to_index(px + dx, py + dy, size)
            if cell in claimed or unsafe(cell, cargo):
                continue
            value = cell_halite(cell)
            if value <= 0:
                continue
            d = abs(dx) + abs(dy)
            score = value / (1.0 + 2.0 * d)
            if dist(cell, focus_cell, size) <= size // 4:
                score *= 1.15
            if score > best_score or (score == best_score and best_cell is not None and cell < best_cell):
                best_score = score
                best_cell = cell
    if best_cell is not None and best_cell != pos:
        move = _step_toward(pos, best_cell, size, cargo, unsafe, claimed, cell_halite)
        if move is not None:
            return move
    if best_cell == pos:
        return None
    safest = _choose(pos, list(DIRECTION_ORDER), size, cargo, unsafe, claimed, cell_halite)
    if safest is None:
        return None
    if unsafe(step_index(pos, safest, size), cargo) and not unsafe(pos, cargo):
        return None
    return safest
