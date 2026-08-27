"""Reusable async websocket harness for cogame-halite seats.

Speaks ``halite/1`` (see ``docs/PROTOCOL.md``), one JSON text message per turn
each way::

    server -> player  {"type":"hello", "seat":2, "alias":"FLEET-CHARLIE", ...}
    player -> server  {"type":"register", "policy":"llm", "label":"..."}
    server -> player  {"type":"observe", "turn":137, ...}
    player -> server  {"type":"orders", "turn":137, "source":"llm", "actions":{...}}
    server -> player  {"type":"done", "result":{...}}

The websocket URL comes from ``COWORLD_PLAYER_WS_URL`` (legacy alias
``COGAMES_ENGINE_WS_URL``).

**The player exits 0 on a dead socket.** A receive loop that raises on a close
frame exits 1 and fails certification (the raid 0.1.3 scar), so every read is
wrapped and a closed socket is a normal end of episode.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from abc import ABC, abstractmethod
from typing import Any

import aiohttp
from aiohttp import WSMsgType

WS_URL_ENV_VARS = ("COWORLD_PLAYER_WS_URL", "COGAMES_ENGINE_WS_URL")

CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_CONNECT_ATTEMPTS = 5
DEFAULT_RECONNECT_DELAY_SECONDS = 0.5

#: Handshake statuses that can never succeed on retry before the first connect.
_FATAL_HTTP_STATUSES = {403: "connection rejected (403): bad slot or token"}


class PlayerError(Exception):
    """Fatal player-side failure (bad auth, server never reachable, bad env)."""


class Policy(ABC):
    """One seat's policy."""

    #: What goes in the ``register`` message.
    policy_name: str = "scripted:tidewalker"
    label: str = ""

    def on_hello(self, hello: dict) -> None:
        """Called once per (re)connection."""

    @abstractmethod
    async def orders(self, observation: dict) -> dict:
        """Return the ``orders`` message body for one ``observe`` frame.

        Must return within the frame's ``deadlineMs``; the harness does not
        police it, the policy does (the whole fallback ladder is player-side
        until step 4).
        """

    def on_done(self, result: dict) -> None:
        """Called when the server sends ``done``."""


def resolve_ws_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for name in WS_URL_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    raise PlayerError(
        "no websocket URL: set " + " or ".join(WS_URL_ENV_VARS)
    )


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def run_player(
    policy: Policy,
    *,
    url: str | None = None,
    max_connect_attempts: int = DEFAULT_MAX_CONNECT_ATTEMPTS,
) -> dict:
    """Play one episode. Returns the final ``result`` (possibly empty)."""
    ws_url = resolve_ws_url(url)
    result: dict[str, Any] = {}
    ever_connected = False
    attempts = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while attempts < max_connect_attempts:
            attempts += 1
            try:
                async with session.ws_connect(ws_url, heartbeat=30.0) as ws:
                    ever_connected = True
                    done, progressed, result = await _play(ws, policy, result)
                    if done:
                        return result
                    # The budget is only reset by a connection that actually
                    # PLAYED. A server that accepts the socket and drops it
                    # again would otherwise reconnect forever and the player
                    # container would never exit (which certification counts as
                    # a failure just as loudly as exiting 1).
                    if progressed:
                        attempts = 0
            except aiohttp.WSServerHandshakeError as exc:
                if ever_connected:
                    log("server has gone away after the episode; exiting 0")
                    return result
                fatal = _FATAL_HTTP_STATUSES.get(exc.status)
                if fatal:
                    raise PlayerError(fatal) from exc
                log(f"handshake failed ({exc.status}); retry {attempts}")
            except (aiohttp.ClientError, OSError) as exc:
                if ever_connected:
                    log(f"socket gone ({exc!r}); episode over, exiting 0")
                    return result
                log(f"connect failed ({exc!r}); retry {attempts}")
            await asyncio.sleep(DEFAULT_RECONNECT_DELAY_SECONDS * attempts)
        if ever_connected:
            return result
        raise PlayerError(f"could not connect to {ws_url} in {max_connect_attempts} attempts")


async def _play(ws, policy: Policy, result: dict) -> tuple[bool, bool, dict]:
    """One connection's message loop.

    Returns ``(episode_finished, made_progress, result)``. ``made_progress`` is
    True once this connection has answered at least one ``observe``.
    """
    registered = False
    progressed = False
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            if msg.type == WSMsgType.ERROR:
                log(f"websocket error frame: {ws.exception()!r}")
                break
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "hello":
                policy.on_hello(data)
                if not registered:
                    registered = True
                    await _send(
                        ws,
                        {
                            "type": "register",
                            "policy": policy.policy_name,
                            "label": policy.label,
                        },
                    )
            elif kind == "observe":
                reply = await policy.orders(data)
                reply.setdefault("type", "orders")
                reply.setdefault("turn", data.get("turn"))
                if not await _send(ws, reply):
                    break
                progressed = True
            elif kind == "done":
                result = data.get("result") or {}
                policy.on_done(result)
                return True, True, result
    except (aiohttp.ClientError, OSError, RuntimeError, asyncio.CancelledError) as exc:
        # A close frame mid-receive is a normal end of episode, never exit 1.
        log(f"receive loop ended: {exc!r}")
    return False, progressed, result


async def _send(ws, message: dict) -> bool:
    try:
        await ws.send_str(json.dumps(message))
        return True
    except (aiohttp.ClientError, OSError, RuntimeError, ConnectionResetError) as exc:
        log(f"send failed ({exc!r}); the server has gone")
        return False


def main(policy_factory) -> int:
    """Entry-point wrapper: always exits 0 unless the seat could never play."""
    try:
        asyncio.run(run_player(policy_factory()))
    except PlayerError as exc:
        log(f"player error: {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0
