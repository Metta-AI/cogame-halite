"""The lockstep episode engine: parallel batch, deadlines, strikes, budget guard.

This is a **simultaneous-decision game**, so the engine asks all four seats at
once: one ``observe`` frame is written to every live seat *before any reply is
awaited*, and the engine then waits on the replies together under **one shared
deadline** (``asyncio.wait`` with a single timeout, never a per-seat loop).
Sequential querying is the documented way to blow the 720 s play budget.

Every wait is bounded. The fallback ladder's server-side half lives here:

4. a late / malformed / wrong-turn / disconnected reply is replaced by the
   orders ``tidewalker`` compiles from the same state, in-process
   (``micro.compile_turn`` — the same function the scripted player imports), a
   ``fallback`` event records the cause, and the sim steps. **The sim never
   waits.**
5. ten consecutive substitutions mark the seat dead: it is no longer awaited
   (so it cannot consume the deadline), it keeps playing ``tidewalker``, and a
   valid reply revives it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from . import defaults, events, micro, results as results_mod
from .config import GameConfig
from .replay import ReplayWriter
from .sim import HaliteGuardError, HaliteSim


class SeatLink(Protocol):
    """One seat's transport. The server implements it over a websocket; tests
    implement it in process."""

    seat: int

    @property
    def connected(self) -> bool: ...

    async def send(self, message: dict) -> None: ...

    async def receive(self) -> dict | None:
        """Next inbound message, or ``None`` when the socket is gone."""


@dataclass
class SeatState:
    seat: int
    link: SeatLink | None = None
    policy: str = ""
    label: str = ""
    registered: bool = False
    llm_turns: int = 0
    strikes: int = 0
    dead: bool = False
    reported_dead: bool = False
    fallbacks: dict[str, int] = field(
        default_factory=lambda: {c: 0 for c in defaults.FALLBACK_CAUSES}
    )
    last_source: str = "scripted"


@dataclass
class EpisodeOutcome:
    results: dict[str, Any]
    replay: ReplayWriter
    end_rule: str
    reason: str
    final_turn: int


class Engine:
    def __init__(
        self,
        config: GameConfig,
        links: list[SeatLink | None],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_player_failure: Callable[[str, int], Awaitable[None]] | None = None,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.config = config
        self.sim = HaliteSim(config)
        self.seats = [SeatState(seat=i, link=links[i]) for i in range(config.num_seats)]
        self.clock = clock
        self.sleep = sleep
        self.on_player_failure = on_player_failure
        self.log = log
        self.started_at = clock()
        self.budget_guard_fired = False
        self.leader: int | None = None
        self.last_directive_open: float | None = None
        #: Instrumentation the engine tests read: one entry per turn, the seats
        #: whose ``observe`` frame was written before any reply was awaited.
        self.batch_trace: list[dict[str, Any]] = []

    # ------------------------------------------------------------- helpers

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started_at

    def _alias(self, seat: int) -> str:
        return defaults.ALIASES[seat]

    def _is_directive_turn(self, turn: int) -> bool:
        return turn % self.config.directive_every == 0

    def _observation(self, seat: int, turn: int, directive: bool, deadline_ms: int) -> dict:
        obs = self.sim.observation(seat)
        return {
            "type": "observe",
            "turn": turn,
            "maxTurns": self.config.episode_steps,
            "directive": directive,
            "deadlineMs": deadline_ms,
            "seat": seat,
            "alias": self._alias(seat),
            "aliases": list(defaults.ALIASES[: self.config.num_seats]),
            "config": self.config.observation_config(),
            "halite": obs["halite"],
            "players": obs["players"],
            "player": seat,
            # Kaggle's observation object, key for key, so a leaderboard bot's
            # `Board(obs, config)` works unchanged on this frame: `Board` reads
            # `step` and `remainingOverageTime` through
            # `helpers.Observation`, and `turn` is our own spelling of `step`.
            # `remainingOverageTime` is always the config default here (the
            # design note's §Out of scope: our pacing is the engine's
            # deadlines, the field is present for shape compatibility).
            "step": obs["step"],
            "remainingOverageTime": obs["remainingOverageTime"],
            "eliminated": list(self.sim.eliminated),
            "board": self.sim.ascii_board(),
            "budget": {
                "elapsedMs": int(self.elapsed * 1000),
                "wallClockBudgetMs": int(self.config.wall_clock_budget_seconds * 1000),
            },
        }

    def _scripted_orders(self, seat: int) -> dict[str, str]:
        """What ``tidewalker`` compiles from the current state, in process."""
        view = micro.BoardView.from_sim(self.sim)
        return micro.compile_turn(view, seat, micro.TIDEWALKER, baseline="tidewalker")

    def _accept(self, seat: int, message: dict, turn: int) -> tuple[dict[str, str], str, list[dict]]:
        """Validate one ``orders`` reply. Returns (actions, source, events).

        Raises :class:`_RejectedReply` with the fallback cause on a reply that
        cannot be used at all.
        """
        if not isinstance(message, dict) or message.get("type") != "orders":
            raise _RejectedReply("malformed", f"type={message.get('type')!r}")
        reply_turn = message.get("turn")
        if reply_turn != turn:
            raise _RejectedReply("wrong_turn", f"turn={reply_turn!r} want {turn}")
        raw = message.get("actions")
        if raw is not None and not isinstance(raw, dict):
            raise _RejectedReply("malformed", "actions is not an object")
        source = message.get("source")
        if source not in defaults.SOURCES:
            source = "scripted"

        actions: dict[str, str] = {}
        for key in sorted((raw or {}), key=_uid_key):
            if len(actions) >= defaults.MAX_ACTIONS_PER_TURN:
                break
            if not isinstance(key, str) or len(key) > defaults.MAX_ASSET_ID_CHARS:
                continue
            value = (raw or {})[key]
            if not isinstance(value, str) or value not in defaults.ALL_ACTIONS:
                continue
            if self.sim.is_ship(seat, key):
                if value not in defaults.SHIP_ACTIONS:
                    continue
            elif self.sim.is_shipyard(seat, key):
                if value not in defaults.SHIPYARD_ACTIONS:
                    continue
            else:
                continue
            actions[key] = value

        turn_events: list[dict] = []
        text = message.get("note")
        if isinstance(text, str) and text.strip():
            turn_events.append(
                events.note(seat, text, source, int(message.get("latencyMs") or 0))
            )
        return actions, source, turn_events

    # ------------------------------------------------------- the turn batch

    async def _collect(self, turn: int) -> tuple[list[dict[str, str]], list[dict], list[str]]:
        """One parallel batch. All ``observe`` frames out, then one shared wait."""
        directive = self._is_directive_turn(turn)
        deadline_ms = (
            self.config.directive_deadline_ms if directive else self.config.turn_deadline_ms
        )
        orders: list[dict[str, str]] = [{} for _ in self.seats]
        turn_events: list[dict] = []
        sources: list[str] = ["fallback"] * len(self.seats)

        guard = self.elapsed >= self.config.budget_guard_seconds
        if guard and not self.budget_guard_fired:
            self.budget_guard_fired = True
            turn_events.append(events.budget_guard(turn))
            self.log(f"BUDGET GUARD at turn {turn} ({self.elapsed:.1f}s) — all seats scripted")

        # A DEAD seat still receives its observe frame -- that is the only way
        # it can answer again -- but it is not AWAITED, so it can never hold up
        # the batch. Its reply (which arrives after this turn's deadline) is
        # picked up by the zero-timeout revival probe at the top of the next
        # batch.
        reachable = [
            s
            for s in self.seats
            if not guard
            and s.link is not None
            and s.link.connected
            and self.sim.eliminated[s.seat] is None
        ]
        await self._probe_dead(reachable)
        awaited = [s for s in reachable if not s.dead]

        if directive and awaited and self.config.directive_spacing_ms > 0:
            # Spacing floor: four calls per batch at >= 10 s spacing is 24
            # req/min, under the sidecar's 30 req/min per-episode cap (the raid
            # 2026-08-23 scar). This floor, not the LLM, sets the typical
            # episode length.
            if self.last_directive_open is not None:
                wait = (
                    self.config.directive_spacing_ms / 1000.0
                    - (self.clock() - self.last_directive_open)
                )
                if wait > 0:
                    await self.sleep(wait)
            self.last_directive_open = self.clock()

        # --- every observe frame is written BEFORE any reply is awaited ----
        sent: list[SeatState] = []
        failed_send: dict[int, str] = {}
        for state in reachable:
            frame = self._observation(state.seat, turn, directive, deadline_ms)
            try:
                await state.link.send(frame)
                sent.append(state)
            except Exception as exc:  # noqa: BLE001 - transport is untrusted
                failed_send[state.seat] = f"{type(exc).__name__}: {exc}"
        self.batch_trace.append(
            {
                "turn": turn,
                "sent": [s.seat for s in sent],
                "awaited": [s.seat for s in sent if not s.dead],
                "directive": directive,
                "guard": guard,
            }
        )

        # --- ONE shared deadline over every reply --------------------------
        replies: dict[int, dict | None] = {}
        errors: dict[int, str] = {}
        live = [state for state in sent if not state.dead]
        if live:
            tasks = {
                asyncio.ensure_future(state.link.receive()): state.seat for state in live
            }
            try:
                done, pending = await asyncio.wait(
                    tasks.keys(), timeout=deadline_ms / 1000.0
                )
            finally:
                pass
            for task in done:
                seat = tasks[task]
                try:
                    replies[seat] = task.result()
                except Exception as exc:  # noqa: BLE001
                    replies[seat] = None
                    errors[seat] = f"{type(exc).__name__}: {exc}"
            for task in pending:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        for state in self.seats:
            seat = state.seat
            if self.sim.eliminated[seat] is not None:
                orders[seat] = {}
                sources[seat] = "fallback"
                continue
            if state not in live:
                cause = (
                    "host_error"
                    if seat in failed_send
                    else ("disconnected" if not guard else "")
                )
                if guard or state.dead:
                    # A dead seat keeps playing tidewalker; it is not awaited,
                    # so it is not a fresh substitution either.
                    orders[seat] = self._scripted_orders(seat)
                    sources[seat] = "fallback"
                    continue
                self._substitute(state, cause or "disconnected", failed_send.get(seat, ""), orders, turn_events)
                sources[seat] = "fallback"
                continue
            message = replies.get(seat)
            if seat not in replies:
                self._substitute(state, "timeout", f"no reply in {deadline_ms}ms", orders, turn_events)
                sources[seat] = "fallback"
                continue
            if message is None:
                detail = errors.get(seat, "socket closed")
                self._substitute(state, "disconnected", detail, orders, turn_events)
                sources[seat] = "fallback"
                continue
            try:
                actions, source, extra = self._accept(seat, message, turn)
            except _RejectedReply as rejected:
                self._substitute(state, rejected.cause, rejected.detail, orders, turn_events)
                sources[seat] = "fallback"
                continue
            orders[seat] = actions
            sources[seat] = source
            turn_events.extend(extra)
            state.strikes = 0
            state.dead = False
            state.last_source = source
            if source in ("llm", "retry"):
                state.llm_turns += 1
        return orders, turn_events, sources

    async def _probe_dead(self, reachable: list["SeatState"]) -> None:
        """Zero-timeout revival probe for struck-out seats.

        A dead seat is never awaited, so its reply -- which lands after the
        deadline of the turn it was asked about -- would otherwise never be
        read. Polling its inbox for zero seconds costs the batch nothing and is
        what makes "a valid reply revives it" true.
        """
        for state in reachable:
            if not state.dead or state.link is None:
                continue
            task = asyncio.ensure_future(state.link.receive())
            done, pending = await asyncio.wait({task}, timeout=0)
            for item in pending:
                item.cancel()
                with contextlib.suppress(BaseException):
                    await item
            for item in done:
                try:
                    message = item.result()
                except Exception:  # noqa: BLE001
                    message = None
                if isinstance(message, dict) and message.get("type") == "orders":
                    state.dead = False
                    state.strikes = 0
                    self.log(
                        f"SEAT {state.seat} ({self._alias(state.seat)}) REVIVED "
                        f"after a valid reply"
                    )

    def _count_fallback(self, state: SeatState, cause: str, turn_events: list[dict], detail: str) -> None:
        state.fallbacks[cause] = state.fallbacks.get(cause, 0) + 1
        turn_events.append(events.fallback(state.seat, cause, detail))

    def _substitute(
        self,
        state: SeatState,
        cause: str,
        detail: str,
        orders: list[dict[str, str]],
        turn_events: list[dict],
    ) -> None:
        orders[state.seat] = self._scripted_orders(state.seat)
        self._count_fallback(state, cause, turn_events, detail)
        state.strikes += 1
        if state.strikes >= defaults.STRIKE_LIMIT and not state.dead:
            state.dead = True
            turn_events.append(events.strike(state.seat))
            self.log(
                f"SEAT {state.seat} ({self._alias(state.seat)}) MARKED DEAD after "
                f"{state.strikes} consecutive substitutions — playing tidewalker"
            )

    # ------------------------------------------------------------- the loop

    async def run(self) -> EpisodeOutcome:
        cfg = self.config
        writer = ReplayWriter(
            coworld="halite",
            seed=cfg.seed,
            config=cfg.replay_config(),
            names=[p.name for p in cfg.players],
            aliases=list(defaults.ALIASES[: cfg.num_seats]),
            policy_sources=[""] * cfg.num_seats,
            colors=list(defaults.COLORS[: cfg.num_seats]),
        )
        self.sim.reset()
        end_rule = "full_time"
        stop_detail = ""
        arrival_events: list[dict] = []
        turn = 0
        try:
            while True:
                if self.elapsed >= cfg.wall_clock_budget_seconds:
                    end_rule = "wall_clock"
                    writer.add_turn(
                        turn,
                        self.sim.replay_turn_state(),
                        [{} for _ in self.seats],
                        arrival_events,
                        self.sim.state_hash(),
                    )
                    break
                final = turn >= cfg.episode_steps - 1
                if final:
                    orders = [{} for _ in self.seats]
                    batch_events: list[dict] = []
                else:
                    orders, batch_events, _sources = await self._collect(turn)
                writer.add_turn(
                    turn,
                    self.sim.replay_turn_state(),
                    orders,
                    arrival_events + batch_events,
                    self.sim.state_hash(),
                )
                if final:
                    end_rule = "full_time"
                    break
                result = self.sim.step(orders)
                arrival_events = list(result.events)
                arrival_events.extend(self._lead_events())
                turn = result.turn
                if result.last_fleet:
                    end_rule = "last_fleet"
                    writer.add_turn(
                        turn,
                        self.sim.replay_turn_state(),
                        [{} for _ in self.seats],
                        arrival_events,
                        self.sim.state_hash(),
                    )
                    break
        except HaliteGuardError as exc:
            end_rule = "fault"
            stop_detail = f"{type(exc).__name__}: {exc}"
            self.log(f"SIM FAULT at turn {turn}: {stop_detail}")
        except Exception as exc:  # noqa: BLE001 - a fault is an outcome
            end_rule = "fault"
            stop_detail = f"{type(exc).__name__}: {exc}"
            self.log(f"ENGINE FAULT at turn {turn}: {stop_detail}")

        final_turn = writer.turns[-1]["t"] if writer.turns else 0
        writer.set_stop(end_rule, final_turn)
        reason = {
            "full_time": "complete",
            "last_fleet": "complete",
            "wall_clock": "deadline",
            "fault": "fault",
        }[end_rule]

        writer.policy_sources = [s.policy or "scripted:tidewalker" for s in self.seats]
        doc = results_mod.build(
            names=[p.name for p in cfg.players],
            aliases=list(defaults.ALIASES[: cfg.num_seats]),
            banked=self.sim.banks(),
            ships=self.sim.ship_counts(),
            yards=self.sim.yard_counts(),
            mined=[s.mined for s in self.sim.stats],
            stolen=[s.stolen for s in self.sim.stats],
            collisions_won=[s.collisions_won for s in self.sim.stats],
            collisions_lost=[s.collisions_lost for s in self.sim.stats],
            eliminated_turn=list(self.sim.eliminated),
            llm_turns=[s.llm_turns for s in self.seats],
            fallbacks=[s.fallbacks for s in self.seats],
            dead_seats=[s.dead for s in self.seats],
            reason=reason,
            end_rule=end_rule,
            final_turn=final_turn,
            seed=cfg.seed,
            episode_steps=cfg.episode_steps,
            stop_detail=stop_detail,
        )
        writer.results = doc
        await self._report_dead_seats()
        return EpisodeOutcome(
            results=doc,
            replay=writer,
            end_rule=end_rule,
            reason=reason,
            final_turn=final_turn,
        )

    def _lead_events(self) -> list[dict]:
        banks = self.sim.banks()
        alive = [s for s in range(self.config.num_seats) if self.sim.eliminated[s] is None]
        if not alive:
            return []
        leader = max(alive, key=lambda s: (banks[s], -s))
        if leader == self.leader:
            return []
        self.leader = leader
        return [events.lead(leader, banks[leader])]

    async def _report_dead_seats(self) -> None:
        if self.on_player_failure is None:
            return
        for state in self.seats:
            if not state.dead or state.reported_dead:
                continue
            state.reported_dead = True
            with contextlib.suppress(Exception):
                await self.on_player_failure(
                    f"seat {state.seat} ({self._alias(state.seat)}) stopped answering "
                    f"after {defaults.STRIKE_LIMIT} consecutive substitutions",
                    state.seat,
                )


class _RejectedReply(Exception):
    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


def _uid_key(uid: object) -> tuple[int, int, str]:
    if not isinstance(uid, str):
        return (10**9, 10**9, str(uid))
    turn, _, n = uid.partition("-")
    try:
        return (int(turn), int(n), uid)
    except ValueError:
        return (10**9, 10**9, uid)
