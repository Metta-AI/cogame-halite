"""aiohttp game server implementing the Coworld runtime contract.

Episode mode (default): read the game config from ``COGAME_CONFIG_URI``, serve
``/player?slot=&token=`` websockets, run one episode with
:class:`cogame_halite.engine.Engine`, then write ``results.json`` to
``COGAME_RESULTS_URI`` and the replay bytes to ``COGAME_SAVE_REPLAY_URI`` and
exit 0.

Certifier probes this server must answer (each has cost a coworld a release):

* ``GET /healthz``.
* ``GET /client/player?slot=&token=`` and ``GET /client/global`` — real pages,
  registered **before** any catch-all asset route, and neither opens a player
  socket (lantern 0.1.1).
* ``/player?slot=&token=`` **closes the socket unless the token matches the
  seat** (cogame-flatland 0.1.1: the certifier probes with a bad token).
* a ``/global`` websocket that emits a first message immediately and keeps
  answering pings for a **20 s shutdown grace** after artifacts are written
  (lantern 0.1.3).

Replay mode: when ``COGAME_LOAD_REPLAY_URI`` is set, no episode runs; the
static viewer bundle and the replay bytes are served for local viewing.

Entry point: ``python -m cogame_halite.server``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import sys
from pathlib import Path

from aiohttp import WSMsgType, web

from . import defaults, results as results_mod, uris
from .config import ConfigError, GameConfig
from .engine import Engine, EpisodeOutcome
from .replay import ReplayWriter
from .version import GAME_VERSION, PROTOCOL

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWER_DIST = REPO_ROOT / "viewer" / "dist"

PLAYER_WS_HEARTBEAT_SECONDS = 30.0
#: Keep /healthz and /global answering this long after artifacts are written.
SHUTDOWN_GRACE_SECONDS = 20.0

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>cogame-halite — {title}</title>
<style>
 body{{background:#16110d;color:#f2e8d8;font:14px/1.5 ui-monospace,Menlo,monospace;margin:0;padding:24px}}
 h1{{font-size:18px;letter-spacing:.14em;text-transform:uppercase;color:#e8a33d;margin:0 0 12px}}
 pre{{background:#0b0805;padding:12px;overflow:auto;max-height:70vh}}
</style></head>
<body><h1>{title}</h1><p>{blurb}</p><pre id="log">connecting…</pre>
<script>
 var log = document.getElementById('log');
 var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') +
                        location.host + {ws_path});
 ws.onmessage = function (e) {{ log.textContent = e.data; }};
 ws.onerror = function () {{ log.textContent = 'websocket error'; }};
 ws.onclose = function () {{ log.textContent += '\\n(closed)'; }};
</script></body></html>
"""

GLOBAL_CLIENT_HTML = _PAGE.format(
    title="global feed",
    blurb="Spectator status feed for this episode. Replays render in the "
    "static wasm bundle, never here.",
    ws_path="'/global'",
)


def player_client_html(slot: int) -> str:
    return _PAGE.format(
        title=f"seat {slot}",
        blurb="Read-only view of one seat. This page never opens the player "
        "socket — the player container owns it.",
        ws_path="'/global'",
    )


class WsSeat:
    """One seat's transport: an aiohttp websocket plus an inbound queue.

    The engine writes every ``observe`` frame before awaiting any reply, then
    waits on all four :meth:`receive` calls under one shared deadline, so the
    inbound side is a queue rather than a per-turn future.
    """

    def __init__(self, seat: int, name: str) -> None:
        self.seat = seat
        self.name = name
        self.ws: web.WebSocketResponse | None = None
        self.ever_connected = False
        self.registered = False
        self.policy = ""
        self.label = ""
        self.inbox: asyncio.Queue = asyncio.Queue()

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def send(self, message: dict) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            raise ConnectionError(f"seat {self.seat} is not connected")
        await ws.send_str(json.dumps(message))

    async def receive(self) -> dict | None:
        item = await self.inbox.get()
        return item

    def deliver(self, data: object) -> None:
        self.inbox.put_nowait(data if isinstance(data, dict) else None)

    def fail_waiters(self) -> None:
        """Unblock a parked receive when the socket dies."""
        self.inbox.put_nowait(None)


class GameServer:
    def __init__(
        self,
        config: GameConfig,
        *,
        results_uri: str | None = None,
        save_replay_uri: str | None = None,
        player_failure_uri: str | None = None,
    ) -> None:
        self.config = config
        self.results_uri = results_uri
        self.save_replay_uri = save_replay_uri
        self.player_failure_uri = player_failure_uri
        self.seats = [
            WsSeat(i, p.name) for i, p in enumerate(config.players)
        ]
        self.results_doc: dict | None = None
        # The lobby is over when every seat has BOTH connected AND registered.
        # Waiting only for the sockets races the register packet: a seat that
        # reconnects a second late lands its `register` after the check and is
        # then wrongly reported as unregistered.
        self._lobby_ready = asyncio.Event()
        self._global_wss: set[web.WebSocketResponse] = set()
        self._failure_reported = False
        self._last_turn = 0

    # ------------------------------------------------------------- routing

    def make_app(self) -> web.Application:
        app = web.Application()
        # /client/* pages come FIRST: a catch-all asset route registered before
        # them makes the certifier's browser-surface probe 404 (lantern 0.1.1).
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/client/global", self._handle_global_client)
        app.router.add_get("/client/player", self._handle_player_client)
        app.router.add_get("/global", self._handle_global)
        app.router.add_get("/player", self._handle_player)
        _add_replay_routes(app, lambda: None)
        return app

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "game": "halite", "gameVersion": GAME_VERSION}
        )

    def _authorized_slot(self, request: web.Request) -> int:
        try:
            slot = int(request.query.get("slot", ""))
        except ValueError:
            raise web.HTTPForbidden(text="bad slot")
        if not 0 <= slot < len(self.seats):
            raise web.HTTPForbidden(text="bad slot")
        token = request.query.get("token", "")
        if not hmac.compare_digest(
            token.encode("utf-8"), self.config.tokens[slot].encode("utf-8")
        ):
            raise web.HTTPForbidden(text="bad token")
        return slot

    async def _handle_global_client(self, request: web.Request) -> web.Response:
        return web.Response(text=GLOBAL_CLIENT_HTML, content_type="text/html")

    async def _handle_player_client(self, request: web.Request) -> web.Response:
        slot = self._authorized_slot(request)
        return web.Response(text=player_client_html(slot), content_type="text/html")

    async def _handle_global(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=PLAYER_WS_HEARTBEAT_SECONDS)
        await ws.prepare(request)
        snapshot = {
            "type": "status",
            "protocol": PROTOCOL,
            "gameVersion": GAME_VERSION,
            "aliases": list(defaults.ALIASES[: self.config.num_seats]),
            "names": [s.name for s in self.seats],
            "maxTurns": self.config.episode_steps,
            "turn": self._last_turn,
            "done": self.results_doc is not None,
        }
        if self.results_doc is not None:
            snapshot["result"] = self.results_doc
        await ws.send_str(json.dumps(snapshot))
        self._global_wss.add(ws)
        try:
            async for _msg in ws:
                pass  # broadcast-only
        finally:
            self._global_wss.discard(ws)
        return ws

    async def _handle_player(self, request: web.Request):
        slot = self._authorized_slot(request)
        seat = self.seats[slot]
        if seat.connected:
            raise web.HTTPConflict(text="slot already connected")
        ws = web.WebSocketResponse(heartbeat=PLAYER_WS_HEARTBEAT_SECONDS)
        await ws.prepare(request)
        seat.ws = ws
        seat.ever_connected = True
        print(f"seat {slot} ({defaults.ALIASES[slot]}) connected", file=sys.stderr)
        await ws.send_str(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": PROTOCOL,
                    "seat": slot,
                    "alias": defaults.ALIASES[slot],
                    "aliases": list(defaults.ALIASES[: self.config.num_seats]),
                    "config": self.config.observation_config(),
                    "maxTurns": self.config.episode_steps,
                    "directiveEvery": self.config.directive_every,
                }
            )
        )
        self._check_lobby()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    seat.deliver(None)
                    continue
                if isinstance(data, dict) and data.get("type") == "register":
                    seat.registered = True
                    seat.policy = str(data.get("policy") or "")[:64]
                    seat.label = defaults.truncate_runes(
                        data.get("label"), defaults.MAX_LABEL_RUNES
                    )
                    print(
                        f"seat {slot} registered policy={seat.policy!r} "
                        f"label={seat.label!r}",
                        file=sys.stderr,
                    )
                    self._check_lobby()
                    continue
                seat.deliver(data)
        finally:
            if seat.ws is ws:
                seat.ws = None
                seat.fail_waiters()
                print(f"seat {slot} disconnected", file=sys.stderr)
        return ws

    def _check_lobby(self) -> None:
        if all(s.connected and s.registered for s in self.seats):
            self._lobby_ready.set()

    # ------------------------------------------------------ the episode

    async def run_episode(self) -> EpisodeOutcome:
        cfg = self.config
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(
                self._lobby_ready.wait(), cfg.player_connect_timeout_seconds
            )
        no_shows = [s for s in self.seats if not s.ever_connected]
        for seat in no_shows:
            print(
                f"SEAT {seat.seat} NEVER CONNECTED - PLAYING tidewalker",
                file=sys.stderr,
            )
        # The grf-football 2026-08-27 scar: a lost register packet made a
        # champion play the default script for a whole episode with no error
        # anywhere. Log loudly and report it.
        unregistered = [s for s in self.seats if s.ever_connected and not s.registered]
        for seat in unregistered:
            print(
                f"SEAT {seat.seat} HAS NO REGISTER RECORD - PLAYING tidewalker",
                file=sys.stderr,
            )
        first_bad = no_shows + unregistered
        if first_bad:
            await self._report_player_failure(
                min(first_bad, key=lambda s: s.seat),
                "never connected" if no_shows else "connected without a register record",
            )

        engine = Engine(
            cfg,
            [seat if seat.ever_connected else None for seat in self.seats],
            on_player_failure=self._engine_player_failure,
            log=lambda message: print(message, file=sys.stderr),
        )
        for seat, state in zip(self.seats, engine.seats):
            state.policy = seat.policy
            state.label = seat.label
            state.registered = seat.registered
        outcome = await engine.run()
        self._last_turn = outcome.final_turn
        self.results_doc = outcome.results
        outcome.replay.policy_sources = [
            seat.policy or "scripted:tidewalker" for seat in self.seats
        ]
        self._log_outcome(outcome)
        await self._broadcast_done(outcome.results)
        await self._write_artifacts(outcome)
        return outcome

    def _log_outcome(self, outcome: EpisodeOutcome) -> None:
        doc = outcome.results
        print(
            f"episode end: reason={doc['reason']} end_rule={doc['end_rule']} "
            f"turn={doc['final_turn']} scores={doc['scores']} "
            f"llm_turns={doc['llm_turns']} dead={doc['dead_seats']}",
            file=sys.stderr,
        )
        for seat, counts in enumerate(doc["fallbacks"]):
            if any(counts.values()):
                print(f"  seat {seat} fallbacks: {counts}", file=sys.stderr)

    async def _engine_player_failure(self, message: str, seat: int) -> None:
        await self._write_failure({"message": message, "failed_policy_index": seat})

    async def _report_player_failure(self, seat: WsSeat, why: str) -> None:
        await self._write_failure(
            {
                "message": f"seat {seat.seat} ({defaults.ALIASES[seat.seat]}) {why}",
                "failed_policy_index": seat.seat,
            }
        )

    async def _write_failure(self, payload: dict) -> None:
        """The platform parses this with a CLOSED schema: exactly
        ``{"message", "failed_policy_index"}``, nothing else."""
        if not self.player_failure_uri or self._failure_reported:
            return
        self._failure_reported = True
        assert set(payload) == {"message", "failed_policy_index"}
        with contextlib.suppress(Exception):
            await uris.write_uri(
                self.player_failure_uri,
                json.dumps(payload).encode("utf-8"),
                "application/json",
            )

    async def _broadcast_done(self, results_doc: dict) -> None:
        message = json.dumps({"type": "done", "result": results_doc})
        for seat in self.seats:
            ws = seat.ws
            if ws is None or ws.closed:
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_str(message), 5.0)
        for ws in tuple(self._global_wss):
            if ws.closed:
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_str(message), 5.0)

    async def _write_artifacts(self, outcome: EpisodeOutcome) -> None:
        errors: list[str] = []

        async def attempt(label: str, uri: str | None, data: bytes, ctype: str) -> None:
            if not uri:
                return
            try:
                await uris.write_uri(uri, data, ctype)
                print(f"wrote {label} ({len(data)} bytes) to {uri}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")
                print(f"FAILED to write {label} to {uri}: {exc}", file=sys.stderr)

        await attempt(
            "results",
            self.results_uri,
            json.dumps(outcome.results).encode("utf-8"),
            "application/json",
        )
        await attempt(
            "replay",
            self.save_replay_uri,
            outcome.replay.to_bytes(),
            "application/json",
        )
        if errors:
            print(f"artifact write errors: {errors}", file=sys.stderr)


# --------------------------------------------------------------------------
# Replay mode + the locally served static bundle
# --------------------------------------------------------------------------


def _add_replay_routes(app: web.Application, get_bytes) -> None:
    async def handle_replay_data(request: web.Request) -> web.Response:
        data = get_bytes()
        if not data:
            raise web.HTTPNotFound(text="no replay loaded")
        return web.Response(body=data, content_type="application/json")

    async def handle_replay_index(request: web.Request) -> web.Response:
        index = VIEWER_DIST / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound(text="replay viewer bundle is not built")
        raise web.HTTPFound("/client/replay/index.html?replay=/replay-data")

    app.router.add_get("/replay-data", handle_replay_data)
    app.router.add_get("/client/replay", handle_replay_index)
    if VIEWER_DIST.is_dir():
        app.router.add_static("/client/replay/", VIEWER_DIST)


def make_replay_app(replay_bytes: bytes) -> web.Application:
    app = web.Application()

    async def handle_healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "mode": "replay"})

    app.router.add_get("/healthz", handle_healthz)
    _add_replay_routes(app, lambda: replay_bytes)
    return app


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def async_main() -> int:
    host = os.environ.get("COGAME_HOST", "0.0.0.0")
    port = int(os.environ.get("COGAME_PORT", "8080"))

    load_replay_uri = os.environ.get("COGAME_LOAD_REPLAY_URI", "")
    if load_replay_uri:
        data = await uris.read_uri(load_replay_uri)
        runner = web.AppRunner(make_replay_app(data))
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"replay mode on {host}:{port} ({len(data)} bytes)", file=sys.stderr)
        while True:
            await asyncio.sleep(3600)

    config_uri = os.environ.get("COGAME_CONFIG_URI", "")
    if not config_uri:
        print("COGAME_CONFIG_URI is required", file=sys.stderr)
        return 2
    try:
        config = GameConfig.from_json(await uris.read_uri(config_uri))
    except ConfigError as exc:
        print(f"bad config: {exc}", file=sys.stderr)
        return 2

    server = GameServer(
        config,
        results_uri=os.environ.get("COGAME_RESULTS_URI"),
        save_replay_uri=os.environ.get("COGAME_SAVE_REPLAY_URI"),
        player_failure_uri=os.environ.get("COGAME_PLAYER_FAILURE_URI"),
    )
    runner = web.AppRunner(server.make_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(
        f"cogame-halite {GAME_VERSION} listening on {host}:{port}; "
        f"seats={config.num_seats} turns={config.episode_steps} seed={config.seed}",
        file=sys.stderr,
    )
    try:
        await server.run_episode()
    finally:
        # Keep /healthz and /global answering through a bounded shutdown grace:
        # the certifier pings /global AFTER the player pods start, and a short
        # episode may already have exited (lantern 0.1.3).
        await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
        await runner.cleanup()
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
