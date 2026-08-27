"""11. Privacy — two name spaces, enforced both ways.

In-game a seat sees only ``FLEET-ALPHA``/``BRAVO``/``CHARLIE``/``DELTA``. The
seats' real policy names appear only in ``results.names``, the replay header's
``names``, the viewer's scorebug plates and the endcard. A seat can never learn
who it is playing.
"""

from __future__ import annotations

import json

from conftest import FakeLink, make_config
from cogame_halite import defaults, micro
from cogame_halite.engine import Engine
from players import llm

REAL_NAMES = ["daveey", "daveey-1", "halite-tidewalker", "halite-corsair"]


async def nosleep(_seconds: float) -> None:
    return None


def contains_a_real_name(blob: str) -> str | None:
    for name in REAL_NAMES:
        if name in blob:
            return name
    return None


async def test_no_observe_frame_carries_a_real_player_name():
    seen: list[dict] = []

    class Recorder(FakeLink):
        async def send(self, message):
            seen.append(message)
            await super().send(message)

    engine = Engine(make_config(episode_steps=30), [Recorder(i) for i in range(4)],
                    sleep=nosleep)
    await engine.run()
    assert seen
    for frame in seen:
        leaked = contains_a_real_name(json.dumps(frame))
        assert leaked is None, f"observe frame leaked {leaked!r}"
        assert frame["alias"] in defaults.ALIASES
        assert frame["aliases"] == list(defaults.ALIASES)


async def test_the_hello_frame_carries_no_real_name():
    from aiohttp.test_utils import TestClient, TestServer
    from cogame_halite.server import GameServer

    game = GameServer(make_config(episode_steps=6))
    client = TestClient(TestServer(game.make_app()))
    await client.start_server()
    try:
        ws = await client.ws_connect("/player?slot=1&token=token-1")
        hello = json.loads((await ws.receive()).data)
        await ws.close()
    finally:
        await client.close()
    assert contains_a_real_name(json.dumps(hello)) is None
    assert hello["alias"] == "FLEET-BRAVO"


def test_the_prompt_carries_only_aliases():
    from cogame_halite.sim import HaliteSim

    sim = HaliteSim(make_config())
    sim.reset()
    observation = {
        "type": "observe", "turn": 20, "maxTurns": 400, "directive": True,
        "deadlineMs": 18000, "seat": 0, "alias": defaults.ALIASES[0],
        "aliases": list(defaults.ALIASES), "config": sim.config.observation_config(),
        "halite": sim.halite, "players": sim.observation(0)["players"], "player": 0,
        "eliminated": [None] * 4, "board": sim.ascii_board(), "directiveEvery": 20,
    }
    prompt = llm.build_prompt(observation, "mine hard", micro.TIDEWALKER)
    assert contains_a_real_name(prompt) is None
    for alias in defaults.ALIASES:
        assert alias in prompt


def test_the_ascii_board_legend_is_letters_not_names():
    from cogame_halite.sim import HaliteSim

    sim = HaliteSim(make_config())
    sim.reset()
    assert contains_a_real_name(sim.ascii_board()) is None


async def test_no_in_board_string_in_the_replay_events_carries_a_real_name():
    engine = Engine(make_config(episode_steps=40),
                    [FakeLink(i, note="pushing north on FLEET-BRAVO") for i in range(4)],
                    sleep=nosleep)
    outcome = await engine.run()
    for turn in outcome.replay.turns:
        for event in turn["events"]:
            leaked = contains_a_real_name(json.dumps(event))
            assert leaked is None, f"event {event['k']} leaked {leaked!r}"


# ----------------------------------------------- the spectator side DOES carry them
async def test_results_and_the_replay_header_do_carry_the_real_names():
    engine = Engine(make_config(episode_steps=20), [FakeLink(i) for i in range(4)],
                    sleep=nosleep)
    outcome = await engine.run()
    assert outcome.results["names"] == REAL_NAMES
    assert outcome.results["aliases"] == list(defaults.ALIASES)
    doc = outcome.replay.document()
    assert doc["names"] == REAL_NAMES
    assert doc["aliases"] == list(defaults.ALIASES)


def test_the_viewer_plate_shows_the_real_name_beside_the_alias():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "client" / "replay_broadcast.html").read_text()
    assert "plate-name" in page
    assert "realName(seat)" in page, "the scorebug plate must show the real policy name"
    assert "shortAlias(seat)" in page
