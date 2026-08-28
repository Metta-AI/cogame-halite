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
async def test_the_engine_budget_opens_before_the_lobby_and_not_at_import(monkeypatch):
    """The lobby waits up to `player_connect_timeout_seconds` (120 s) BEFORE
    the engine exists, so a budget that starts when the engine is constructed
    bounds the turns but not the episode: the anchor has to be taken before
    the lobby.

    It must NOT be process start (r2-F7): the platform may reuse a warm
    container or start this process long before it hands it an episode, and
    time the process spent idle is not the episode's to spend."""
    import time

    from cogame_halite import server as server_module

    captured: dict = {}
    real_engine = server_module.Engine

    class Recorder(real_engine):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(server_module, "Engine", Recorder)
    monkeypatch.setattr(server_module, "PROCESS_STARTED_AT", time.monotonic() - 3600.0)
    game = GameServer(make_config(episode_steps=4, player_connect_timeout_seconds=1.0))
    opened = time.monotonic()
    outcome = await game.run_episode()

    assert outcome.reason == "complete"
    assert captured["started_at"] == game.started_at, "the engine gets the server's anchor"
    assert captured["started_at"] >= opened, "the anchor is this episode's, not the process's"
    # Taken BEFORE the 1 s lobby the engine is constructed after, so the lobby
    # is spent inside the guard and the hard stop.
    assert captured["started_at"] < opened + 0.5


async def test_an_episode_in_an_old_process_still_starts_with_a_full_budget(monkeypatch):
    """r2-F7, reproduced: with both budgets anchored at import, a process older
    than the 660 s hard stop settled its very first episode at turn 0 —
    `reason=deadline end_rule=wall_clock turn=0` — because `elapsed` was
    already past the stop before the first turn was played."""
    import time

    from cogame_halite import server as server_module

    monkeypatch.setattr(server_module, "PROCESS_STARTED_AT", time.monotonic() - 700.0)
    game = GameServer(make_config(episode_steps=6, player_connect_timeout_seconds=1.0))
    outcome = await asyncio.wait_for(game.run_episode(), 60)

    assert outcome.reason == "complete" and outcome.end_rule == "full_time"
    assert outcome.final_turn == 5, "every turn was played, not just turn 0"


def test_the_worst_case_container_time_fits_inside_the_platform_pin():
    """720 s = 60% of the platform's 1200 s episode timeout. Worst case, from
    the instant the episode begins: the hard stop, one in-flight directive
    turn, the artifact phase and the shutdown grace.

    Every term is read from the constant that enforces it, and the two
    assumptions the sum rests on are asserted rather than assumed: the
    in-flight turn is ONE deadline, and the directive spacing floor cannot add
    to it."""
    from cogame_halite.server import ARTIFACT_WRITE_BUDGET_SECONDS

    pin = defaults.PLATFORM_EPISODE_TIMEOUT_MINUTES * 60 * 0.6
    worst = (
        defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
        + defaults.DEFAULT_DIRECTIVE_DEADLINE_MS / 1000.0
        + ARTIFACT_WRITE_BUDGET_SECONDS
        + SHUTDOWN_GRACE_SECONDS
    )
    assert pin == 720
    assert worst == 718
    assert worst <= pin, f"worst modelled container time {worst}s exceeds the {pin}s pin"
    # And the lobby is INSIDE the hard stop, not added to it.
    assert (
        defaults.DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS
        < defaults.DEFAULT_BUDGET_GUARD_SECONDS
        < defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
    )

    # Assumption 1: the in-flight turn costs ONE deadline. The engine's observe
    # writes are bounded and share the turn's budget with the replies (r2-F2),
    # so a socket that will not drain cannot add a second deadline to the turn
    # that is in flight when the hard stop trips.
    engine_source = (REPO / "server" / "cogame_halite" / "engine.py").read_text()
    assert "budget = deadline_ms / 1000.0" in engine_source
    assert "asyncio.wait_for(state.link.send(frame), budget)" in engine_source
    assert "timeout=budget" in engine_source

    # Assumption 2: the directive spacing floor never adds to that turn. It is
    # only slept while the budget guard is OFF (past the guard no seat is asked
    # and no batch is paced), so the last paced turn opens before the guard and
    # is over well inside the hard stop.
    assert (
        defaults.DEFAULT_BUDGET_GUARD_SECONDS
        + defaults.DEFAULT_DIRECTIVE_SPACING_MS / 1000.0
        + defaults.DEFAULT_DIRECTIVE_DEADLINE_MS / 1000.0
        <= defaults.DEFAULT_WALL_CLOCK_BUDGET_SECONDS
    ), "a spaced directive turn can still be open when the hard stop trips"
    assert "if not guard" in engine_source, "the guard empties `reachable`"


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


# ------------------------------------------------- a peer that stops reading
async def test_a_player_that_stops_reading_its_socket_cannot_stall_run_episode(tmp_path):
    """r2-F2: a peer that holds its socket open and stops draining it applies
    flow control back to `ws.send_str`, which parks on the transport's drain
    waiter with no timeout of its own — reproduced in the r2 review with a raw
    socket and a 2 KB receive buffer, where `run_episode()` never returned.

    The write shares the turn's deadline, so the episode settles, the seat is
    substituted and the artifacts are written."""
    results = tmp_path / "results.json"
    game = GameServer(
        make_config(
            episode_steps=8,
            turn_deadline_ms=100,
            directive_deadline_ms=200,
            player_connect_timeout_seconds=1.0,
        ),
        results_uri=f"file://{results}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    unblock = asyncio.Event()
    try:
        ws = await client.ws_connect("/player?slot=0&token=token-0")
        await ws.receive()  # hello

        async def never_drains(_payload: str) -> None:
            await unblock.wait()

        game.seats[0].ws.send_str = never_drains
        started = asyncio.get_running_loop().time()
        outcome = await asyncio.wait_for(game.run_episode(), 60)
        spent = asyncio.get_running_loop().time() - started
        unblock.set()
        await ws.close()
    finally:
        unblock.set()
        await client.close()

    assert outcome.reason == "complete"
    assert outcome.results["fallbacks"][0]["disconnected"] > 0
    # Bounded end to end: the blocked write costs one turn deadline and the
    # `done` broadcast to the same socket is capped at 5 s.
    assert spent < 20.0, f"run_episode took {spent:.1f}s with one blocked peer"
    assert json.loads(results.read_text())["reason"] == "complete"
