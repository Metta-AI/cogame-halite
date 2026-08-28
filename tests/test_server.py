"""10. Server routes — the certifier's probe surface, in the order it probes.

Each of these has cost a coworld a release at least once, so each is named with
its scar.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_config
from cogame_halite import defaults
from cogame_halite.server import SHUTDOWN_GRACE_SECONDS, GameServer

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def server():
    game = GameServer(make_config(episode_steps=8))
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    try:
        yield game, client
    finally:
        await client.close()


async def test_healthz(server):
    _game, client = server
    response = await client.get("/healthz")
    assert response.status == 200
    body = await response.json()
    assert body["status"] == "ok" and body["game"] == "halite"


async def test_client_pages_are_real_and_open_no_player_socket(server):
    """The episode runner probes /healthz, GET /client/player?slot=&token=, a
    bad-token player websocket and GET /client/global BEFORE starting player
    pods (lantern 0.1.1)."""
    game, client = server
    response = await client.get("/client/global")
    assert response.status == 200
    assert "text/html" in response.headers["Content-Type"]
    assert len(await response.text()) > 200

    response = await client.get("/client/player", params={"slot": "1", "token": "token-1"})
    assert response.status == 200
    assert "text/html" in response.headers["Content-Type"]
    assert all(not seat.connected for seat in game.seats), (
        "a /client/ page must never open the player socket"
    )


async def test_client_player_is_token_checked(server):
    _game, client = server
    assert (await client.get("/client/player", params={"slot": "0", "token": "nope"})).status == 403


async def test_a_bad_player_token_is_rejected(server):
    """The certifier probes with a wrong token (cogame-flatland 0.1.1)."""
    _game, client = server
    with pytest.raises(Exception):
        await client.ws_connect("/player?slot=0&token=bad")
    with pytest.raises(Exception):
        await client.ws_connect("/player?slot=9&token=token-0")
    with pytest.raises(Exception):
        await client.ws_connect("/player?slot=notanint&token=token-0")


async def test_the_right_token_connects_and_gets_hello(server):
    _game, client = server
    ws = await client.ws_connect("/player?slot=2&token=token-2")
    hello = json.loads((await ws.receive()).data)
    assert hello["type"] == "hello"
    assert hello["seat"] == 2
    assert hello["alias"] == "FLEET-CHARLIE"
    assert hello["aliases"] == list(defaults.ALIASES)
    assert hello["maxTurns"] == 8
    assert hello["directiveEvery"] == defaults.DEFAULT_DIRECTIVE_EVERY
    assert "tokens" not in json.dumps(hello)
    await ws.close()


async def test_one_connection_per_slot(server):
    _game, client = server
    ws = await client.ws_connect("/player?slot=0&token=token-0")
    with pytest.raises(Exception):
        await client.ws_connect("/player?slot=0&token=token-0")
    await ws.close()


async def test_global_emits_a_first_message_immediately(server):
    """The platform runner requires a first message on /global."""
    _game, client = server
    ws = await client.ws_connect("/global")
    message = await asyncio.wait_for(ws.receive(), 5)
    snapshot = json.loads(message.data)
    assert snapshot["type"] == "status"
    assert snapshot["aliases"] == list(defaults.ALIASES)
    await ws.close()


async def test_global_answers_a_ping(server):
    """The runner pings /global (2 s deadline) AFTER the player pods start
    (lantern 0.1.3)."""
    _game, client = server
    # autoping=False so the PONG reaches receive() instead of being swallowed
    # by the client's own auto-handler.
    ws = await client.ws_connect("/global", autoping=False)
    await ws.receive()
    await ws.ping(b"probe")
    pong = await asyncio.wait_for(ws.receive(), 5)
    assert pong.type.name == "PONG"
    await ws.close()


def test_the_shutdown_grace_is_twenty_seconds():
    assert SHUTDOWN_GRACE_SECONDS >= 20.0
    source = (REPO / "server" / "cogame_halite" / "server.py").read_text()
    assert 'add_get("/client/replay' not in source, "no /client/replay route"
    assert "add_static" not in source, "the pod never serves the viewer bundle"
    assert "VIEWER_DIST" not in source, "the pod never reaches for viewer/dist"
