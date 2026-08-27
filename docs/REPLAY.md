# Replay format & viewer contract

A replay is a single **UTF-8 JSON** document (extension `.replay`), written
once at the end of the episode and also streamable to disk turn by turn. It is
**self-sufficient by construction**: everything the viewer needs is in the
bytes, and the viewer contacts S3 and nothing else.

## The document

```json
{"format":"cogame-halite-replay","version":1,"gameVersion":"1.0.0","protocol":"halite/1",
 "coworld":"halite","seed":8675309,
 "config":{ …every resolved game-config field, tokens excluded… },
 "names":["daveey","daveey-1","halite-tidewalker","halite-corsair"],
 "aliases":["FLEET-ALPHA","FLEET-BRAVO","FLEET-CHARLIE","FLEET-DELTA"],
 "policySources":["llm","llm","scripted:tidewalker","scripted:corsair"],
 "colors":["#e8a33d","#3fb6b0","#c65fa8","#8fbf3f"],
 "turns":[{"t":0,
           "halite":[<441 integers, round(cell)>],
           "players":[[<bank>,{"<yardId>":<pos>},{"<shipId>":[<pos>,<cargo>]}], …4],
           "orders":[{"<assetId>":"<ACTION>"}, …4],
           "events":[…],
           "hash":"9f2a41c07be31d55"}, …],
 "results":{ …the full results document… },
 "stop":{"rule":"full_time","turn":399}}
```

* The per-turn `halite` array is **rounded to integers** because that is what is
  drawn. The exact float state is pinned by `hash`, and by `orders` + `seed`,
  which let CI re-derive the episode exactly.
* `hash` is **FNV-1a 64** over a canonical encoding — `turn`, then each cell's
  halite formatted `%.3f` in index order, then per seat the `bank`, the sorted
  `(yard_id, pos)` pairs and the sorted `(ship_id, pos, cargo)` triples, then
  the elimination turns. It sorts assets, so it pins *state*, not serialisation
  order, and it is stable across processes (no `PYTHONHASHSEED` dependency).
* `stop` is **one load-bearing record applied by the same constructor on record
  and on re-derive**. A wall-clock stop is a wall-clock fact that cannot be
  recomputed from sim state, so it is recorded, not inferred.
  `tests/test_replay.py` runs the record → re-derive check for **every** end
  reason, not just `complete`.
* Size: roughly 3 kB/turn, so ≈ 1.3 MB for a 400-turn episode and ≈ 400 kB for
  the 120-turn CI fixture.

## Events

One `events` array per turn, each `{"k": <kind>, …}`, in the order the
resolution produced them. This is the complete vocabulary
(`server/cogame_halite/events.py`); `tests/test_replay.py` schema-checks every
kind and `tests/test_viewer.py` asserts a `.beat-marker.<kind>` CSS rule for
every kind that places a scrubber beat.

| kind | payload | drawn as |
|---|---|---|
| `spawn` | `seat, ship, pos` | a pop at the yard, +1 on the seat's ship count |
| `convert` | `seat, ship, yard, pos` | the new dock stamps in, scrubber **beat** (`yard`) |
| `deposit` | `seat, ship, yard, pos, amount` | coin arc into the plate, bank ticks up |
| `mine` | `seat, ship, pos, amount` | the cell dims one step, the hull pip grows |
| `collide` | `pos, survivor:{seat,ship}\|null, lost:[{seat,ship,cargo}], stolen` | flash + shards, feed line, scrubber **beat** (`ram`) |
| `yardraze` | `pos, yardSeat, yard, shipSeat, ship` | the dock cracks, scrubber **beat** (`raze`) |
| `eliminate` | `seat, turn` | the plate greys out, scrubber **beat** (`out`) |
| `lead` | `seat, bank` | the crown moves, scrubber **beat** (`lead`); emitted only on a change |
| `note` | `seat, text (≤140 runes), source, latencyMs` | a speech line in the feed |
| `fallback` | `seat, cause, detail (≤120 runes)` | a small grey chip in the feed |
| `strike` | `seat` | the plate is marked "silent" |
| `budget_guard` | `turn` | a feed line, scrubber **beat** (`guard`) |
| `stop` | `rule, turn` | the endcard's win-condition chip |

## Strict UTF-8

Every string that can reach the replay is truncated on **rune** boundaries
(`server/cogame_halite/defaults.py::truncate_runes`). A byte-boundary
truncation splits a multi-byte character and produces bytes that render in a
browser and fail a strict parser; `tests/test_replay.py` feeds emoji-laden
notes through the whole path and parses the result with `bytes.decode("utf-8")`
(strict) plus `json.loads`.

## The viewer bundle

`game.replay_viewer.bundle = "static-replay-viewer"`, built by
`tools/build_replay_viewer.sh` (the `coworld build` hook) from the Dockerfile's
`wasm-builder` stage. The bundle is:

```
index.html                      client/replay_broadcast.html
chrome_common.js                coworld-ctf, BYTE-FOR-BYTE
broadcast_core.js               coworld-ctf, BYTE-FOR-BYTE (loaded by the Worker only)
static_replay.js                coworld-ctf + the three documented adaptations
static_replay_worker.js         coworld-ctf + the three documented adaptations
halite_replay.{js,wasm,data}    replay-viewer/halite_replay.nim -> emscripten
font.ttf                        the Rajdhani face the chrome's @font-face loads
```

The three adaptations, the ones `cogame-factorio` already made to the same four
files:

1. `start()` takes the replay bytes the page fetched (one fetch serves both the
   chrome and the renderer),
2. the sim mismatch-tick attribute is gone — **nothing is re-simulated in the
   browser**; the wasm renderer draws the *recorded* per-turn state,
3. the exported symbols are renamed `_halite_*`.

The page owns playback (which turn) and tells the renderer on the sprite
protocol's text channel: `s:<turn>` and `r:<0|1>` (the cargo-at-risk overlay).
Chrome JSON rides the reserved sprite 4090's label.

`data-replay-loaded="true"` lands on `<html>` on the first drawn frame and
`data-replay-error="<message>"` on failure; the `coworld-replay` bridge `ready`
message is posted from a `MutationObserver` callback that fires **after**
`data-replay-loaded` is set, never on rAF timing at the call site.

### What it draws

* **Board.** 21 × 21 cells on the tiling seabed. Each cell's halite is a
  crystal sprite at one of six density steps (0, 1–49, 50–149, 150–299,
  300–449, 450–500). Ships are the seat-coloured hull with a **cargo pip**
  whose size and glow scale with cargo; shipyards are the seat-coloured dock.
* **Cargo-at-risk overlay.** A ship is **at risk** iff some enemy ship with
  cargo **≤** its own is within torus Manhattan distance 1 — the exact
  predicate the ram rule uses next turn. At-risk ships get a pulsing red halo
  and the ≤ 4 cells a lighter enemy could ram them from get a faint red wash.
  Always on; `r` toggles it.
* **Scorebug.** Four plates, two per column: colour swatch, alias, **real
  policy name**, banked halite (big, tabular), cargo afloat, ships, yards,
  halite at risk, a crown on the leader, a grey wash on an eliminated seat.
* **Clock.** `TURN 137 / 399` plus the caption `mining` / `hauling` /
  `raiding`, derived from the turn's event mix.
* **Feed.** The last ~12 events in words, plus each LLM `note` as a speech line
  under its alias.
* **Endcard.** Final standings 1–4 with alias + real name, banked, mined,
  stolen, rams won/lost, ships built, elimination turn and the win-condition
  chip from `stop.rule`.
* **Playback.** 125 ms per turn at 1× (a 400-turn episode plays in 50 s; the
  120-turn CI fixture in 15 s, which outlasts the 12 s viewer soak), speeds
  from the inherited chrome.
* **Legible at 360 px wide.** A stated acceptance property, checked by
  `viewer_smoke.mjs` through `tools/ci/narrow_fixture.html` as well as at
  desktop size, and by `tools/ci/renderer_fixture.html` at three canvas sizes
  with a full-cap 140-rune note on every seat.
