# Working in this repo

Conventions for agents (and humans) making changes to `cogame-halite`. The
design note is [`docs/plans/2026-08-27-halite-design.md`](docs/plans/2026-08-27-halite-design.md);
the rules and the port's evidence are [`docs/RULES.md`](docs/RULES.md); the wire
protocol is [`docs/PROTOCOL.md`](docs/PROTOCOL.md); the replay format and the
viewer contract are [`docs/REPLAY.md`](docs/REPLAY.md).

## The two inviolable rules

1. **`vendor/upstream/` is byte-pristine.** It is the vendored
   `kaggle-environments` Halite IV source at the pinned commit
   (`vendor/UPSTREAM.md` records the release, the commit and a sha256 per
   file). Never edit anything under it. There are **zero patches**; the only
   thing added is three package `__init__.py` files from `sim/shim/`, copied in
   by `sim/assemble.py`, and `vendor/PATCHES.md` explains why. The two upstream
   code paths that are *not* imported — the `populate_board` adapter and the
   12-line elimination block — are named there with their citations.
2. **The fidelity gate is inviolable.** `tests/test_fidelity.py` proves the
   served sim is bit-identical to a real `kaggle-environments==1.32.7` install
   over 8 seeds x 399 turns (exact floats, dict insertion order, statuses,
   rewards) plus 50-seed board generation. It must pass after every
   sim-touching change. If it fails, **the port is wrong, never the test.**
   Weakening or skipping it is a failed task, not a passing build.

## Where things live

- **Rule constants are read from the vendored `halite.json`**, never re-typed:
  `server/cogame_halite/defaults.py::upstream_defaults()`, with
  `tests/test_vendor.py` asserting every mirror. A re-vendor that changes
  `collectRate` fails there, loudly.
- Server-contract values (deadlines, the strike rule, the budget guard, the
  caps) live in `defaults.py` and nowhere else.
- `server/cogame_halite/micro.py` is the scripted baseline **and** the executor
  of every LLM directive. `players/halite_player.py` imports it and so does the
  engine's fallback path, so the two can never drift.
- **Results keys are a CLOSED schema** in three places:
  `server/cogame_halite/results.py::RESULTS_KEYS`, the manifest's
  `results_schema` (`additionalProperties: false`) and the smoke assertion in
  `.github/workflows/ci.yml` (which imports `RESULTS_KEYS` from the code).
  Adding a results field means updating all three; `tests/test_results.py` is
  the tripwire.
- **`num_agents` = 4** in every variant's `game_config` and in
  `certification.game_config`, never at a variant's top level.
- **Every string that can reach the replay is truncated on RUNE boundaries**
  (`defaults.truncate_runes`): `note` 140, `register.label` 40,
  `results.stop_detail` 200, a `fallback` event's detail 120. A byte-boundary
  truncation splits a multi-byte character and produces replay bytes that
  render in a browser and fail a strict UTF-8 parser.
- **Two name spaces.** In-game: `FLEET-ALPHA/BRAVO/CHARLIE/DELTA` only. Real
  policy names live in `results.names`, the replay header's `names`, the
  scorebug plates and the endcard. `tests/test_privacy.py` enforces both ways.
- **Degrade, never hang.** Every wait is bounded: the lobby, the shared
  per-turn deadline, the directive spacing floor, the strike rule, the budget
  guard at 600 s and the hard stop at 660 s — both measured from **the instant
  the episode begins** (`GameServer.run_episode` takes the anchor before the
  lobby, so the lobby is spent inside them; a budget anchored at process start
  spends however long the platform kept a warm container alive) —
  plus a 20 s cap on the artifact phase and the 20 s shutdown grace. Worst
  case 660 + 18 + 20 + 20 = 718 s, inside the 720 s pin. Bad player input is a
  substitution, never a crash. A sim fault is an *outcome*
  (`results.reason = "fault"`), and the artifacts are still written.
- **The viewer is a static wasm bundle, never a pod.** `client/chrome_common.js`
  and `client/broadcast_core.js` are coworld-ctf's, **byte-for-byte**;
  `replay-viewer/static_replay{,_worker}.js` and `replay-viewer/config.nims`
  come from the same starter, because the emscripten link flags and the JS
  bootstrap are a matched pair. `client/replay_broadcast.html` is ctf's page
  plus one appended game block under a banner comment that names every removal.

## Build / test / package

```sh
uv sync --frozen                       # runtime + dev
uv sync --frozen --group fidelity      # + the CI-only differential gate
uv run python sim/assemble.py --check  # vendor + shim -> build/khalite
uv run pytest                          # the whole suite, fidelity gate included

bash viewer/build_viewer.sh            # -> viewer/dist (needs nim 2.2.4 + emcc 4.0.15)
docker build --platform=linux/amd64 -t coworld-halite:local .
./tools/ci/docker_smoke.sh coworld-halite:local
```

`build/`, `dist/`, `viewer/dist/` and `replay-viewer/dist/` are gitignored
build outputs. `tools/ci/docker_smoke.sh` and `tools/build_replay_viewer.sh`
are committed **executable (0755)** — `coworld build` hard-requires `os.X_OK`
on the hook, and a `bash SCRIPT` invocation would hide a missing bit.

Commit in small, single-purpose units with pathspec `git add`. TDD for
behaviour changes: failing test first, then the implementation.

## Art

`data/art/` is committed, generated once from the committed source sheets in
`scripts/art/source/` (nano-banana / `gemini-2.5-flash-image`, see
`playbooks/art-nanobanana.md`) by `scripts/art/split_art_sheets.py`. CI does not
regenerate art. Re-running the split script must reproduce the same files.

## Coworld platform contract

`docs/PROTOCOL.md` is normative. The certifier probes surfaces the lockstep
design does not otherwise need — `GET /client/player?slot=&token=`,
`GET /client/global`, a bad-token player websocket that must be **closed**, and
a `/global` websocket that emits a first message and keeps answering pings for
a 20 s shutdown grace after the artifacts are written. `tests/test_server.py`
covers each with the scar it comes from.

Uploads are `coworld-release.yml` (`workflow_dispatch`). The step order is
load-bearing: build -> certify -> **upload policies** -> `upload-coworld`
(wait for the hosted smoke) -> **`secret put` after** `upload-coworld`, with
the secret namespace equal to `game.name` (`halite`).
