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
    assert "await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)" in source, (
        "the constant is not the contract: the server must AWAIT the grace"
    )
    assert 'add_get("/client/replay' not in source, "no /client/replay route"
    assert "add_static" not in source, "the pod never serves the viewer bundle"
    assert "VIEWER_DIST" not in source, "the pod never reaches for viewer/dist"


# ----------------------------------------------------------- failure payload
async def test_the_player_failure_payload_is_exactly_two_keys(tmp_path):
    target = tmp_path / "player_failure.json"
    game = GameServer(
        make_config(episode_steps=4, player_connect_timeout_seconds=1.0),
        player_failure_uri=f"file://{target}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    try:
        await game.run_episode()
    finally:
        await client.close()
    payload = json.loads(target.read_text())
    assert set(payload) == {"message", "failed_policy_index"}
    assert payload["failed_policy_index"] == 0


async def test_a_seat_that_never_registers_is_logged_and_reported(tmp_path, capsys):
    """The grf-football 2026-08-27 scar: a lost register packet made a champion
    play the default script for a whole episode with no error anywhere."""
    target = tmp_path / "player_failure.json"
    game = GameServer(
        make_config(episode_steps=4, player_connect_timeout_seconds=1.0),
        player_failure_uri=f"file://{target}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    sockets = []
    try:
        for slot in range(4):
            ws = await client.ws_connect(f"/player?slot={slot}&token=token-{slot}")
            await ws.receive()  # hello; deliberately never register
            sockets.append(ws)
        await game.run_episode()
    finally:
        for ws in sockets:
            await ws.close()
        await client.close()
    err = capsys.readouterr().err
    assert "HAS NO REGISTER RECORD" in err
    payload = json.loads(target.read_text())
    assert set(payload) == {"message", "failed_policy_index"}


# ------------------------------------------------------------- the artifacts
async def test_a_full_episode_writes_results_and_a_replay(tmp_path):
    results = tmp_path / "results.json"
    replay = tmp_path / "replay.json"
    game = GameServer(
        make_config(episode_steps=12, player_connect_timeout_seconds=1.0),
        results_uri=f"file://{results}",
        save_replay_uri=f"file://{replay}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    try:
        outcome = await game.run_episode()
    finally:
        await client.close()
    assert outcome.reason == "complete"
    doc = json.loads(results.read_text())
    from cogame_halite.results import RESULTS_KEYS

    assert tuple(doc) == RESULTS_KEYS
    from cogame_halite.replay import parse

    parse(replay.read_bytes())


async def test_done_is_broadcast_before_the_artifacts_are_written(tmp_path):
    results = tmp_path / "results.json"
    game = GameServer(
        make_config(episode_steps=6, player_connect_timeout_seconds=1.0),
        results_uri=f"file://{results}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    try:
        ws = await client.ws_connect("/global")
        await ws.receive()
        task = asyncio.ensure_future(game.run_episode())
        seen = None
        while seen is None:
            message = await asyncio.wait_for(ws.receive(), 30)
            payload = json.loads(message.data)
            if payload.get("type") == "done":
                seen = payload
        await task
        await ws.close()
    finally:
        await client.close()
    assert seen["result"]["reason"] == "complete"


async def test_replay_mode_serves_the_recorded_bytes(tmp_path):
    from cogame_halite.server import make_replay_app

    payload = b'{"format":"cogame-halite-replay"}'
    client = TestClient(TestServer(make_replay_app(payload)))
    await client.start_server()
    try:
        assert (await client.get("/healthz")).status == 200
        response = await client.get("/replay-data")
        assert await response.read() == payload
    finally:
        await client.close()


# --------------------------------------------------- the wall-clock budget
async def test_the_engine_budget_is_measured_from_process_start(monkeypatch):
    """The lobby waits up to `player_connect_timeout_seconds` (120 s) BEFORE
    the engine exists. A budget that starts when the engine is constructed
    bounds the episode but not the container, and the platform's timeout is on
    the container. The server therefore hands the engine process start."""
    import time

    from cogame_halite import server as server_module

    captured: dict = {}
    real_engine = server_module.Engine

    class Recorder(real_engine):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(server_module, "Engine", Recorder)
    game = GameServer(make_config(episode_steps=4, player_connect_timeout_seconds=1.0))
    outcome = await game.run_episode()

    assert outcome.reason == "complete"
    assert captured["started_at"] == server_module.PROCESS_STARTED_AT
    assert captured["started_at"] < time.monotonic(), "process start is in the past"


def test_the_worst_case_container_time_fits_inside_the_platform_pin():
    """720 s = 60% of the platform's 1200 s episode timeout. Worst case, from
    process start: the hard stop, one in-flight directive turn, the artifact
    phase and the shutdown grace."""
    from cogame_halite.server import ARTIFACT_WRITE_BUDGET_SECONDS

    pin = defaults.PLATFORM_EPISODE_TIMEOUT_MINUTES * 60 * 0.6
    worst = (
        defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
        + defaults.DEFAULT_DIRECTIVE_DEADLINE_MS / 1000.0
        + ARTIFACT_WRITE_BUDGET_SECONDS
        + SHUTDOWN_GRACE_SECONDS
    )
    assert pin == 720
    assert worst <= pin, f"worst modelled container time {worst}s exceeds the {pin}s pin"
    # And the lobby is INSIDE the hard stop, not added to it.
    assert (
        defaults.DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS
        < defaults.DEFAULT_BUDGET_GUARD_SECONDS
        < defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
    )


async def test_a_hanging_artifact_write_cannot_outlive_its_budget(monkeypatch, tmp_path):
    """`uris.write_uri` is bounded per attempt (3 x 30 s + backoff); two
    artifacts at that bound is ~182 s of container time, which is what pushed
    the worst case past the pin. The phase itself is capped."""
    from cogame_halite import server as server_module

    async def never(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server_module.uris, "write_uri", never)
    monkeypatch.setattr(server_module, "ARTIFACT_WRITE_BUDGET_SECONDS", 0.2)
    game = GameServer(
        make_config(episode_steps=4, player_connect_timeout_seconds=1.0),
        results_uri=f"file://{tmp_path / 'results.json'}",
        save_replay_uri=f"file://{tmp_path / 'replay.json'}",
    )
    outcome = await asyncio.wait_for(game.run_episode(), 30)
    assert outcome.reason == "complete"
