# Wire protocol — `halite/1`

Normative. The design is `docs/plans/2026-08-27-halite-design.md`; the rules
are `docs/RULES.md`.

## Runtime contract

The game container reads its config from `COGAME_CONFIG_URI` and writes
`COGAME_RESULTS_URI`, `COGAME_SAVE_REPLAY_URI` and (on a seat failure)
`COGAME_PLAYER_FAILURE_URI`. `COGAME_LOAD_REPLAY_URI` puts it in replay mode.
`COGAME_HOST` / `COGAME_PORT` (default `0.0.0.0:8080`) bind the server.

Player containers read `COWORLD_PLAYER_WS_URL` (legacy alias
`COGAMES_ENGINE_WS_URL`).

### Routes

| Route | What it does |
|---|---|
| `GET /healthz` | `{"status":"ok","game":"halite","gameVersion":"..."}` |
| `GET /client/player?slot=&token=` | A real HTML page. Token-checked. **Never opens the player socket.** |
| `GET /client/global` | A real HTML page for the spectator feed. |
| `WS /player?slot=&token=` | One seat. **Closed unless the token matches the seat.** One connection per slot. |
| `WS /global` | Broadcast-only. Emits a first message immediately, answers pings for a 20 s shutdown grace after the artifacts are written. |
| `GET /replay-data`, `/client/replay/` | Local replay viewing (the static bundle). |

## Message flow

1. **`hello`** — server → player, on connect:

   ```json
   {"type":"hello","protocol":"halite/1","seat":2,"alias":"FLEET-CHARLIE",
    "aliases":["FLEET-ALPHA","FLEET-BRAVO","FLEET-CHARLIE","FLEET-DELTA"],
    "config":{…resolved rule config, no tokens…},"maxTurns":400,"directiveEvery":20}
   ```

2. **`register`** — player → server, once, within the lobby:

   ```json
   {"type":"register","policy":"llm"|"scripted:tidewalker"|"scripted:corsair","label":"…≤40 runes"}
   ```

   A seat that finishes the lobby without one is logged at ERROR as
   `SEAT <n> HAS NO REGISTER RECORD - PLAYING tidewalker` and reported to
   `COGAME_PLAYER_FAILURE_URI`.

3. **`observe`** — server → **all four sockets before any reply is awaited**,
   every turn.

4. **`orders`** — player → server, one per `observe`.

5. **`done`** — server → player after the last turn, then a bounded flush and
   close. **Players exit 0 on a dead socket.**

## The observation

```json
{"type":"observe","turn":137,"maxTurns":400,"directive":false,"deadlineMs":400,
 "seat":2,"alias":"FLEET-CHARLIE","aliases":["FLEET-ALPHA","FLEET-BRAVO","FLEET-CHARLIE","FLEET-DELTA"],
 "config":{"size":21,"episodeSteps":400,"startingHalite":24000,"spawnCost":500,"convertCost":500,
           "moveCost":0.0,"collectRate":0.25,"regenRate":0.02,"maxCellHalite":500},
 "halite":[<441 numbers, index = (size-y-1)*size + x>],
 "players":[[<bank>,{"<yardId>":<pos>},{"<shipId>":[<pos>,<cargo>]}], … four …],
 "player":2,
 "eliminated":[null,null,null,312],
 "board":"<21 lines of the vendored Board.__str__>",
 "budget":{"elapsedMs":41230,"wallClockBudgetMs":660000}}
```

* **`halite`, `players`, `player` and the turn index are Kaggle's own
  `observation` object, key for key**, so a Kaggle bot's `Board(obs, config)`
  works unchanged. Everything else sits alongside it, never inside it.
* **Asset ordering is part of the contract**: `players[p][1]` and
  `players[p][2]` are serialised in upstream's insertion order, and that is the
  order spawns and converts are processed in.
* **Nothing about the board is hidden** — Halite IV is a perfect-information
  game. What *is* hidden: the other seats' **identities** (aliases only,
  always) and the other seats' **orders for the current turn** (the engine
  writes all four `observe` frames before awaiting any reply).

## The reply

```json
{"type":"orders","turn":137,"source":"llm",
 "actions":{"120-3":"NORTH","0-3":"SPAWN","95-1":"CONVERT"},
 "intent":"raid",
 "note":"squeezing BRAVO off the north cluster"}
```

| Field | Type | Cap / domain | On violation |
|---|---|---|---|
| `turn` | int | must equal the current turn | counted `wrong_turn`, treated as a miss |
| `source` | string | `llm` \| `retry` \| `scripted` \| `fallback` | defaults to `scripted` |
| `actions` | object | **≤ 256 entries**; keys ≤ **24 chars**; values ∈ `{NORTH,SOUTH,EAST,WEST,CONVERT,SPAWN}` | over 256 → first 256 by ascending uid kept; an unknown key/value, an id the seat does not own, or an action illegal for that asset kind → that entry dropped |
| `intent` | string | `mine` \| `expand` \| `raid` \| `defend` \| `hold` | dropped |
| `note` | string | **≤ 140 runes**, spectator-facing | **truncated on a rune boundary** |

**Every string that can reach the replay is truncated on rune boundaries,
never byte boundaries** — `note` (140), `register.label` (40),
`results.stop_detail` (200), a `fallback` event's detail (120).

## Pacing, deadlines and the fallback ladder

This is a **simultaneous-decision** game. One `observe` frame is written to
every live seat before any reply is awaited, and the engine then waits on the
replies together under **one shared deadline** (`asyncio.wait` with a single
timeout, never a per-seat loop).

| Knob | Value | What it bounds |
|---|---|---|
| `turn_deadline_ms` | 400 | the shared wait on a micro turn |
| `directive_deadline_ms` | 18 000 | the shared wait on a directive turn (the player's own 12 s attempt + 5 s retry + transport) |
| `directive_spacing_ms` | 10 000 | the floor between directive batches — 4 calls / batch at ≥ 10 s is 24 req/min, under the Bedrock sidecar's 30 req/min per-episode cap |
| `player_connect_timeout_seconds` | 120 | the lobby |
| `budget_guard_seconds` | 600 | past this the engine asks nobody anything; every seat plays the in-process `tidewalker` compile and the episode still ends `complete` |
| `wall_clock_budget_seconds` | 660 | hard stop at a turn boundary → `reason = "deadline"`, settled by the same scoring ladder |

Per seat, per turn, in order — every step bounded:

1. **Attempt 1.** On a directive turn the player calls the LLM with a 12 s
   client-side timeout; on a micro turn it answers from its compiled plan.
2. **Retry once (player-side).** 5 s, shortened prompt, logs `will retry`
   (never `falling back`).
3. **Player-side scripted fallback.** The player keeps its previous directive,
   compiles orders from it and answers **within the deadline** with
   `source: "scripted"` and a `note` naming the cause.
4. **Server-side scripted fallback.** A late, malformed, wrong-turn or
   disconnected reply is replaced by the orders `tidewalker` compiles from the
   same state, in-process; a `fallback` event records the cause and the sim
   steps. **The sim never waits.**
5. **Strike rule.** Ten consecutive substitutions mark the seat dead: it is no
   longer awaited (so it cannot hold up the batch), it keeps playing
   `tidewalker`, and a valid reply revives it. Dead seats are reported once to
   `COGAME_PLAYER_FAILURE_URI` and land in `results.dead_seats`.

`results.fallbacks[s]` is an object with exactly the keys
`{timeout, malformed, wrong_turn, disconnected, host_error}`, partitioning the
substitutions.

## Artifacts

`COGAME_PLAYER_FAILURE_URI` receives a **closed** payload — exactly
`{"message", "failed_policy_index"}`, nothing else.

`COGAME_RESULTS_URI` receives the closed results document
(`server/cogame_halite/results.py::RESULTS_KEYS`), which is mirrored by the
manifest's `results_schema` and by the smoke assertion in `ci.yml`.

`COGAME_SAVE_REPLAY_URI` receives the replay document — see
[`docs/REPLAY.md`](REPLAY.md).
