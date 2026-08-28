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
   waits.** The seat's ``observe`` **write** shares that same deadline: a peer
   that holds its socket open but stops reading it parks the write on the
   transport's drain waiter, so the frame is cut off at the deadline, the seat
   counts as ``disconnected`` and its link is dropped.
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
    #: Action entries this seat sent BEYOND the 256-entry cap and that were
    #: therefore dropped (the design note's reply-cap table: "over 256 ->
    #: first 256 by ascending uid kept, rest dropped **and counted**"). It is
    #: an audit counter, not a fallback cause: the reply itself was used.
    dropped_over_cap: int = 0
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
        started_at: float | None = None,
    ) -> None:
        self.config = config
        self.sim = HaliteSim(config)
        self.seats = [SeatState(seat=i, link=links[i]) for i in range(config.num_seats)]
        self.clock = clock
        self.sleep = sleep
        self.on_player_failure = on_player_failure
        self.log = log
        #: The instant both budgets are measured from. The server passes
        #: **process start**, so the lobby (up to
        #: ``player_connect_timeout_seconds`` = 120 s) is spent inside the
        #: budget rather than before it — otherwise the 720 s platform pin
        #: bounds neither. Defaults to now for in-process callers (tests).
        self.started_at = clock() if started_at is None else started_at
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

        # The reply cap: over 256 entries, the first 256 by ascending uid are
        # kept and the rest are dropped AND COUNTED, so a policy that floods
        # the wire shows up in the log and in `seats[s].dropped_over_cap`
        # instead of being silently trimmed. It is an audit counter, not a
        # fallback cause -- the reply itself is used.
        ordered = sorted((raw or {}), key=_uid_key)
        if len(ordered) > defaults.MAX_ACTIONS_PER_TURN:
            over = len(ordered) - defaults.MAX_ACTIONS_PER_TURN
            state = self.seats[seat]
            first = state.dropped_over_cap == 0
            state.dropped_over_cap += over
            if first:
                self.log(
                    f"SEAT {seat} ({self._alias(seat)}) sent {len(ordered)} "
                    f"action entries on turn {turn}; {over} over the "
                    f"{defaults.MAX_ACTIONS_PER_TURN} cap were dropped "
                    f"(counted in dropped_over_cap)"
                )
            ordered = ordered[: defaults.MAX_ACTIONS_PER_TURN]

        actions: dict[str, str] = {}
        for key in ordered:
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
        # The writes are BOUNDED, and they share this turn's deadline with the
        # replies. A peer that holds its socket open but stops reading it
        # applies flow control all the way back to ``ws.send_str``, which then
        # parks on the transport's drain waiter with no timeout of its own: an
        # unbounded write here stalls the episode forever, strike rule and
        # budget guard included, because neither is evaluated again until the
        # batch returns. One budget for the whole batch also keeps the
        # worst-case turn at ``deadlineMs``, which is the number the 720 s
        # container pin is computed from.
        budget = deadline_ms / 1000.0
        blocked_write = False
        sent: list[SeatState] = []
        failed_send: dict[int, tuple[str, str]] = {}
        for state in reachable:
            frame = self._observation(state.seat, turn, directive, deadline_ms)
            try:
                await asyncio.wait_for(state.link.send(frame), budget)
                sent.append(state)
            except (asyncio.TimeoutError, TimeoutError):
                if budget > 0.0:
                    # THIS is the seat that would not drain: half a frame is on
                    # its wire and the peer is not reading. The link is
                    # unusable, and asking it again next turn would spend
                    # another deadline, so it is dropped here -- the seat then
                    # takes the ordinary `disconnected` path (one substitution
                    # per turn, then the strike rule), which is what a peer
                    # that stopped reading is.
                    budget = 0.0
                    blocked_write = True
                    state.link = None
                    failed_send[state.seat] = (
                        "disconnected",
                        f"the socket did not drain inside the {deadline_ms}ms deadline",
                    )
                    self.log(
                        f"SEAT {state.seat} ({self._alias(state.seat)}) STOPPED "
                        f"READING its socket on turn {turn}; the write was cut off "
                        f"at {deadline_ms}ms and the seat is substituted from here"
                    )
                else:
                    # Collateral: the turn's deadline was already spent on the
                    # seat above, so this frame never went out. Nothing is
                    # wrong with THIS peer, so its link survives and the cause
                    # is the host's -- next turn it is asked again as usual.
                    failed_send[state.seat] = (
                        "host_error",
                        "the turn deadline was spent on a socket that would not drain",
                    )
            except Exception as exc:  # noqa: BLE001 - transport is untrusted
                failed_send[state.seat] = ("host_error", f"{type(exc).__name__}: {exc}")
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
                done, pending = await asyncio.wait(tasks.keys(), timeout=budget)
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
                cause, detail = failed_send.get(seat, ("disconnected", ""))
                if guard or state.dead:
                    # A dead seat keeps playing tidewalker; it is not awaited,
                    # so it is not a fresh substitution either.
                    orders[seat] = self._scripted_orders(seat)
                    sources[seat] = "fallback"
                    continue
                self._substitute(state, cause, detail, orders, turn_events)
                sources[seat] = "fallback"
                continue
            message = replies.get(seat)
            if seat not in replies:
                # A blocked write spends the batch's deadline, so a seat it
                # deprived did not time out -- the host never gave it a window.
                if blocked_write:
                    self._substitute(
                        state,
                        "host_error",
                        "the turn deadline was spent on a socket that would not drain",
                        orders,
                        turn_events,
                    )
                else:
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
        """One payload, naming every dead seat.

        The platform's player-failure payload is CLOSED —
        ``{"message", "failed_policy_index"}`` — so it carries exactly one
        seat index, and the channel is a URI write: a second write replaces
        the first rather than adding to it. Reporting per seat would therefore
        *lose* the earlier failure. Instead the lowest dead seat is the
        reported index (it struck out first) and the message names them all;
        every dead seat is also in ``results.dead_seats`` and in the ``strike``
        events."""
        if self.on_player_failure is None:
            return
        dead = [s for s in self.seats if s.dead and not s.reported_dead]
        if not dead:
            return
        for state in dead:
            state.reported_dead = True
        who = ", ".join(f"{s.seat} ({self._alias(s.seat)})" for s in dead)
        plural = "seats" if len(dead) > 1 else "seat"
        with contextlib.suppress(Exception):
            await self.on_player_failure(
                f"{plural} {who} stopped answering after "
                f"{defaults.STRIKE_LIMIT} consecutive substitutions",
                dead[0].seat,
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
