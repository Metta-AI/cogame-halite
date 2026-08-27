"""6. Players — env switching, the directive repair table, rune truncation, the
retry ladder's wording, and exit-0 on a closed socket."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_config
from cogame_halite import defaults, micro
from players import llm
from players.halite_player import HalitePolicy

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("PLAYER_PROMPT", "PLAYER_SCRIPTED", "USE_BEDROCK", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------ env switching
def test_player_scripted_selects_the_named_baseline(monkeypatch):
    monkeypatch.setenv("PLAYER_SCRIPTED", "corsair")
    policy = HalitePolicy()
    assert policy.baseline == "corsair"
    assert policy.policy_name == "scripted:corsair"
    assert policy.llm is None


def test_an_unrecognised_baseline_falls_back_to_tidewalker(monkeypatch):
    monkeypatch.setenv("PLAYER_SCRIPTED", "kraken")
    policy = HalitePolicy()
    assert policy.baseline == defaults.DEFAULT_BASELINE == "tidewalker"


def test_neither_env_var_plays_tidewalker():
    policy = HalitePolicy()
    assert policy.baseline == "tidewalker"
    assert policy.policy_name == "scripted:tidewalker"


def test_player_prompt_makes_an_llm_seat(monkeypatch):
    monkeypatch.setenv("PLAYER_PROMPT", "Play the bank.")
    policy = HalitePolicy()
    assert policy.policy_name == "llm"
    assert policy.llm is not None


def test_use_bedrock_is_what_attaches_the_sidecar(monkeypatch):
    """The cogolf 2026-08-24 scar: PLAYER_PROMPT alone gets no Bedrock sidecar
    and the seat silently plays scripted."""
    monkeypatch.setenv("PLAYER_PROMPT", "x")
    assert llm.Provider().available is False
    monkeypatch.setenv("USE_BEDROCK", "true")
    assert llm.Provider().available is True


def test_every_llm_policy_in_policies_json_carries_use_bedrock():
    rows = json.loads((REPO / "tools" / "ci" / "policies.json").read_text())
    for row in rows:
        env = row["env"]
        if "PLAYER_PROMPT" in env:
            assert env.get("USE_BEDROCK") == "true", f"{row['name']} has no USE_BEDROCK"


# --------------------------------------------------------------- the model
def test_the_model_pins_match_the_design_note():
    assert llm.MODEL == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert llm.MAX_TOKENS == 900, "400 truncates: 'cut off at max_tokens'"
    source = (REPO / "players" / "llm.py").read_text()
    assert "output_config=" not in source, "Haiku 4.5 rejects output_config.effort"
    assert "effort" not in source.replace("output_config.effort", "")
    assert "MUST begin with the character {" in llm.SYSTEM_PROMPT


# ------------------------------------------------------- directive repair
def base_observation(turn: int = 0) -> dict:
    from cogame_halite.sim import HaliteSim

    sim = HaliteSim(make_config())
    sim.reset()
    obs = sim.observation(0)
    return {
        "type": "observe",
        "turn": turn,
        "maxTurns": 400,
        "directive": True,
        "deadlineMs": 18000,
        "seat": 0,
        "alias": defaults.ALIASES[0],
        "aliases": list(defaults.ALIASES),
        "config": sim.config.observation_config(),
        "halite": obs["halite"],
        "players": obs["players"],
        "player": 0,
        "eliminated": [None] * 4,
        "board": sim.ascii_board(),
        "directiveEvery": 20,
        "budget": {"elapsedMs": 0, "wallClockBudgetMs": 660000},
    }


def repair(raw: dict, previous=micro.TIDEWALKER):
    return llm.repair(raw, previous, list(defaults.ALIASES), 0, 400)


def test_every_clamp_in_the_repair_table():
    out = repair({"spawnUntil": 9999, "yards": 99, "mineFloor": -50, "returnAt": 1})
    assert out.spawnUntil == 400
    assert out.yards == 4
    assert out.mineFloor == 0
    assert out.returnAt == 50
    low = repair({"spawnUntil": -5, "yards": 0, "mineFloor": 9000, "returnAt": 100000})
    assert low.spawnUntil == 0 and low.yards == 1
    assert low.mineFloor == 500 and low.returnAt == 1500


def test_enum_values_are_lower_cased_and_unknown_ones_keep_the_previous():
    previous = micro.Directive(stance="raid", focus="NE")
    assert repair({"stance": "MINE"}, previous).stance == "mine"
    assert repair({"stance": "sing"}, previous).stance == "raid"
    assert repair({"focus": "nw"}, previous).focus == "NW"
    assert repair({"focus": "MIDDLE"}, previous).focus == "NE"


def test_avoid_must_be_an_opponent_alias_or_becomes_null():
    assert repair({"avoid": "FLEET-BRAVO"}).avoid == "FLEET-BRAVO"
    assert repair({"avoid": "FLEET-ALPHA"}).avoid is None, "a seat cannot avoid itself"
    assert repair({"avoid": "nobody"}).avoid is None
    assert repair({"avoid": 7}).avoid is None


def test_unknown_fields_are_dropped_and_missing_ones_inherit():
    previous = micro.Directive(stance="defend", spawnUntil=120, yards=3,
                               mineFloor=222, returnAt=333, focus="SW", avoid=None)
    out = repair({"nonsense": 1, "stance": "raid"}, previous)
    assert out.stance == "raid"
    assert (out.spawnUntil, out.yards, out.mineFloor, out.returnAt, out.focus) == (
        120, 3, 222, 333, "SW")
    assert not hasattr(out, "nonsense")


def test_turn_zero_defaults_are_the_design_note_table():
    d = micro.TIDEWALKER
    assert (d.stance, d.spawnUntil, d.yards, d.mineFloor, d.returnAt, d.focus, d.avoid) == (
        "mine", 300, 2, 100, 500, "CENTER", None)


@pytest.mark.parametrize(
    "text",
    [
        '{"stance":"raid"}',
        'Sure! {"stance":"raid"} hope that helps.',
        '```json\n{"stance":"raid"}\n```',
        '{"stance":"raid"}\n\nI will also spawn a lot.',
        'prose first {"a":{"b":1},"stance":"raid"} trailing',
    ],
)
def test_json_extraction_tolerates_fences_and_trailing_prose(text):
    raw = llm.extract_json(text)
    assert raw is not None and raw.get("stance") == "raid"


def test_extraction_returns_none_when_there_is_no_object():
    assert llm.extract_json("no json here") is None
    assert llm.extract_json("") is None
    assert llm.extract_json("[1,2,3]") is None


# ------------------------------------------------------- rune truncation
EMOJI = "\U0001F6A2"  # a 4-byte ship


@pytest.mark.parametrize(
    "limit",
    [defaults.MAX_NOTE_RUNES, defaults.MAX_LABEL_RUNES,
     defaults.MAX_STOP_DETAIL_RUNES, defaults.MAX_FALLBACK_DETAIL_RUNES],
)
def test_rune_truncation_never_splits_a_four_byte_character(limit):
    text = EMOJI * (limit * 2)
    out = defaults.truncate_runes(text, limit)
    assert len(out) == limit
    # The whole point: the bytes must survive a STRICT utf-8 round trip.
    assert out.encode("utf-8").decode("utf-8") == out
    assert json.loads(json.dumps({"t": out}))["t"] == out


def test_note_truncation_at_the_cap_in_the_repair_path():
    note = EMOJI * 400
    out = repair({"note": note})
    assert len(out.note) == defaults.MAX_NOTE_RUNES
    assert out.note.encode("utf-8").decode("utf-8") == out.note


def test_a_lone_surrogate_is_scrubbed_not_carried():
    """json.loads accepts \\ud800; str.encode('utf-8') then rejects it."""
    poisoned = json.loads('"a\\ud800b"')
    out = defaults.truncate_runes(poisoned, 100)
    assert out.encode("utf-8").decode("utf-8") == out


# ------------------------------------------------------------ retry ladder
class FailingProvider(llm.Provider):
    def __init__(self, failures: int, reply: str = '{"stance":"raid","note":"hunting"}'):
        super().__init__()
        self.use_bedrock = True
        self.failures = failures
        self.reply = reply
        self.calls = 0

    async def complete(self, prompt: str, timeout: float) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("provider timed out")
        return self.reply


async def test_attempt_one_succeeds_with_source_llm():
    client = llm.DirectiveClient("strategy", FailingProvider(0))
    directive, source, cause = await client.directive(base_observation())
    assert source == "llm" and cause == "" and directive.stance == "raid"


async def test_the_single_retry_logs_will_retry_not_falling_back(capsys):
    client = llm.DirectiveClient("strategy", FailingProvider(1))
    _directive, source, _cause = await client.directive(base_observation())
    err = capsys.readouterr().err
    assert source == "retry"
    assert "will retry" in err
    assert "falling back" not in err, (
        "only a GENUINE fallback may say 'falling back' — the phase-60 grep "
        "distinguishes them (pommerman 0.1.1)"
    )


async def test_a_genuine_fallback_keeps_the_previous_directive_and_says_so(capsys):
    client = llm.DirectiveClient("strategy", FailingProvider(9))
    client.last = micro.Directive(stance="defend", mineFloor=222)
    directive, source, cause = await client.directive(base_observation())
    err = capsys.readouterr().err
    assert source == "scripted"
    assert directive.stance == "defend" and directive.mineFloor == 222
    assert cause
    assert "will retry" in err and "falling back" in err


async def test_an_unparseable_reply_advances_the_ladder():
    client = llm.DirectiveClient("s", FailingProvider(0, reply="I decline to answer."))
    _directive, source, cause = await client.directive(base_observation())
    assert source == "scripted" and cause


async def test_no_provider_answers_immediately_with_the_previous_directive():
    client = llm.DirectiveClient("strategy")
    assert not client.available
    directive, source, cause = await client.directive(base_observation())
    assert source == "scripted" and directive is micro.TIDEWALKER
    assert "no LLM provider" in cause


def test_attempt_timeouts_are_the_design_note_values():
    assert llm.ATTEMPT1_TIMEOUT_SECONDS == 12.0
    assert llm.RETRY_TIMEOUT_SECONDS == 5.0


# ------------------------------------------------------------------ prompt
def test_the_prompt_carries_the_board_the_rules_and_the_strategy():
    prompt = llm.build_prompt(base_observation(20), "MY STANDING ORDERS", micro.TIDEWALKER)
    assert "TURN 20/400" in prompt
    assert "DIRECTIVE TURN" in prompt
    assert "YOU ARE FLEET-ALPHA" in prompt
    assert "MY STANDING ORDERS" in prompt
    assert "the LIGHTER one survives" in prompt
    assert '"stance":"expand|mine|raid|defend"' in prompt


def test_the_strategy_is_rune_truncated_into_the_prompt():
    prompt = llm.build_prompt(base_observation(), EMOJI * 5000, None)
    assert EMOJI * defaults.MAX_STRATEGY_RUNES in prompt
    assert EMOJI * (defaults.MAX_STRATEGY_RUNES + 1) not in prompt


# ------------------------------------------------------------- orders reply
async def test_the_player_answers_bounded_legal_orders_on_a_micro_turn(monkeypatch):
    monkeypatch.setenv("PLAYER_SCRIPTED", "corsair")
    policy = HalitePolicy()
    obs = base_observation(7)
    obs["directive"] = False
    reply = await policy.orders(obs)
    assert reply["type"] == "orders" and reply["turn"] == 7
    assert reply["source"] == "scripted"
    assert len(reply["actions"]) <= defaults.MAX_ACTIONS_PER_TURN
    assert reply["intent"] in defaults.INTENTS
    assert all(v in defaults.ALL_ACTIONS for v in reply["actions"].values())


async def test_an_llm_seat_answers_within_the_deadline_when_the_provider_dies(monkeypatch):
    monkeypatch.setenv("PLAYER_PROMPT", "prompt")
    monkeypatch.setenv("USE_BEDROCK", "true")
    policy = HalitePolicy()
    policy.llm = llm.DirectiveClient("prompt", FailingProvider(9))
    reply = await asyncio.wait_for(policy.orders(base_observation(0)), 5.0)
    assert reply["source"] == "scripted"
    assert "note" in reply and len(reply["note"]) <= defaults.MAX_NOTE_RUNES


# ------------------------------------------------------------- exit codes
EXIT_SCRIPT = r"""
import asyncio, json, sys, os, threading
sys.path[:0] = [{server!r}, {repo!r}]
from aiohttp import web
from players import client as pclient
from players.halite_player import HalitePolicy

ready = threading.Event()
port = [0]

async def handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str(json.dumps({{"type": "hello", "seat": 0, "alias": "FLEET-ALPHA",
                                  "aliases": ["FLEET-ALPHA","FLEET-BRAVO","FLEET-CHARLIE","FLEET-DELTA"],
                                  "config": {{}}, "maxTurns": 400, "directiveEvery": 20}}))
    await asyncio.sleep(0.2)
    # Slam the socket shut mid-receive: a receive loop that RAISES on a close
    # frame exits 1 and fails certification (the raid 0.1.3 scar).
    await ws.close(code=1006)
    return ws

def serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def start():
        app = web.Application(); app.router.add_get("/player", handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port[0] = runner.addresses[0][1]
        ready.set()
        while True:
            await asyncio.sleep(3600)

    loop.run_until_complete(start())

threading.Thread(target=serve, daemon=True).start()
ready.wait(30)
os.environ["COWORLD_PLAYER_WS_URL"] = f"ws://127.0.0.1:{{port[0]}}/player"
sys.exit(pclient.main(HalitePolicy))
"""


def test_the_player_exits_zero_when_the_socket_closes_mid_receive(tmp_path):
    script = tmp_path / "exit_probe.py"
    script.write_text(EXIT_SCRIPT.format(server=str(REPO / "server"), repo=str(REPO)))
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120, cwd=str(REPO)
    )
    assert result.returncode == 0, (
        f"player exited {result.returncode}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )


def test_the_player_exits_one_when_it_can_never_connect(monkeypatch):
    monkeypatch.setenv("COWORLD_PLAYER_WS_URL", "ws://127.0.0.1:1/player")
    from players import client as pclient

    assert pclient.main(HalitePolicy) == 1


def test_a_missing_ws_url_is_a_player_error(monkeypatch):
    monkeypatch.delenv("COWORLD_PLAYER_WS_URL", raising=False)
    monkeypatch.delenv("COGAMES_ENGINE_WS_URL", raising=False)
    from players import client as pclient

    with pytest.raises(pclient.PlayerError):
        pclient.resolve_ws_url()


def test_the_legacy_ws_url_alias_is_accepted(monkeypatch):
    monkeypatch.delenv("COWORLD_PLAYER_WS_URL", raising=False)
    monkeypatch.setenv("COGAMES_ENGINE_WS_URL", "ws://legacy/player")
    from players import client as pclient

    assert pclient.resolve_ws_url() == "ws://legacy/player"
