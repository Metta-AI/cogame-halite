"""14. The lobby — connect AND register, bounded, without a race.

The grf-football 2026-08-27 scar wants a loud complaint when a seat plays a
whole episode without a `register` record. Checking for it the instant the last
socket connects instead races the register packet: a seat that reconnects a
second late (the game container is not up when the player pods start, so the
first connect attempt is refused) lands its `register` after the check and is
wrongly reported. So the lobby is over when every seat is BOTH connected and
registered, bounded by `player_connect_timeout_seconds`.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from conftest import make_config
from cogame_halite.server import GameServer


async def connect(client, slot: int, *, register: bool = True, delay: float = 0.0):
    if delay:
        await asyncio.sleep(delay)
    ws = await client.ws_connect(f"/player?slot={slot}&token=token-{slot}")
    await ws.receive()  # hello
    if register:
        await ws.send_str(json.dumps({
            "type": "register", "policy": "scripted:tidewalker", "label": "tidewalker"}))
    return ws


async def test_a_late_register_does_not_trip_the_no_register_report(tmp_path, capsys):
    """The exact CI failure: seat 0's connect is refused twice, it arrives last,
    and its register lands a beat after the socket."""
    target = tmp_path / "player_failure.json"
    game = GameServer(
        make_config(episode_steps=6, player_connect_timeout_seconds=10.0),
        player_failure_uri=f"file://{target}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    sockets = []
    try:
        for slot in (1, 2, 3):
            sockets.append(await connect(client, slot))
        # Seat 0 arrives late, exactly like a player that had to retry.
        sockets.append(await connect(client, 0, delay=0.3))
        outcome = await game.run_episode()
    finally:
        for ws in sockets:
            await ws.close()
        await client.close()
    err = capsys.readouterr().err
    assert "HAS NO REGISTER RECORD" not in err
    assert not target.exists(), (
        f"a player failure was reported for a healthy lobby: {target.read_text()}"
    )
    assert outcome.reason == "complete"


async def test_the_lobby_still_ends_at_the_timeout_when_a_seat_never_registers(tmp_path):
    target = tmp_path / "player_failure.json"
    game = GameServer(
        make_config(episode_steps=6, player_connect_timeout_seconds=1.0),
        player_failure_uri=f"file://{target}",
    )
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    sockets = []
    try:
        for slot in (0, 1, 2):
            sockets.append(await connect(client, slot))
        sockets.append(await connect(client, 3, register=False))
        outcome = await asyncio.wait_for(game.run_episode(), 60)
    finally:
        for ws in sockets:
            await ws.close()
        await client.close()
    payload = json.loads(target.read_text())
    assert payload["failed_policy_index"] == 3
    assert set(payload) == {"message", "failed_policy_index"}
    # And the episode still ran to its natural end.
    assert outcome.reason == "complete"


async def test_the_lobby_does_not_wait_for_a_seat_that_never_connects(tmp_path):
    game = GameServer(make_config(episode_steps=6, player_connect_timeout_seconds=1.0))
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    sockets = []
    try:
        for slot in (0, 1, 2):
            sockets.append(await connect(client, slot))
        outcome = await asyncio.wait_for(game.run_episode(), 60)
    finally:
        for ws in sockets:
            await ws.close()
        await client.close()
    assert outcome.reason == "complete"
    assert outcome.results["fallbacks"][3]["disconnected"] > 0


async def test_a_fully_registered_lobby_starts_without_waiting_out_the_timeout():
    game = GameServer(make_config(episode_steps=6, player_connect_timeout_seconds=60.0))
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    sockets = []
    try:
        for slot in range(4):
            sockets.append(await connect(client, slot))
        started = asyncio.get_running_loop().time()
        outcome = await asyncio.wait_for(game.run_episode(), 60)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        for ws in sockets:
            await ws.close()
        await client.close()
    assert outcome.reason == "complete"
    assert elapsed < 30, "the lobby waited out the connect timeout on a healthy table"
