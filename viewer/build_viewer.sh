#!/usr/bin/env bash
# Build the static wasm replay viewer into viewer/dist:
#
#   index.html                       the board page (client/replay_broadcast.html)
#   halite_replay.{js,wasm,data}     Nim -> emscripten renderer (replay-viewer/)
#                                    + preloaded data/ (the nano-banana art)
#   static_replay.js,
#   static_replay_worker.js          page <-> Worker glue (coworld-ctf, 3 adaptations)
#   broadcast_core.js                Bitworld sprite-protocol compositor (ctf, verbatim)
#   chrome_common.js                 shared replay chrome (ctf, BYTE-FOR-BYTE)
#   font.ttf                         the Rajdhani face the chrome's @font-face loads
#
# Runs locally (nim + emcc on PATH, packages synced with
# `nimby --global sync nimby.lock`) and inside the Dockerfile's wasm-builder
# stage (cwd = repo root). Ends with a test -f / negative-grep guard chain so a
# half-built bundle never ships.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

NIM="${NIM:-nim}"
if ! command -v "$NIM" >/dev/null 2>&1; then
    if [ -x "$HOME/.nimby/nim/bin/nim" ]; then NIM="$HOME/.nimby/nim/bin/nim";
    else echo "error: nim not found on PATH (nimby use 2.2.4)" >&2; exit 1; fi
fi
if ! command -v emcc >/dev/null 2>&1; then
    echo "error: emcc not found on PATH - install emscripten (emsdk 4.0.15)" >&2
    exit 1
fi
for asset in data/art/seabed.png data/art/halite_crystals.png \
             data/art/hull_alpha.png data/art/hull_bravo.png \
             data/art/hull_charlie.png data/art/hull_delta.png \
             data/art/yard_alpha.png data/art/yard_bravo.png \
             data/art/yard_charlie.png data/art/yard_delta.png; do
    test -f "$asset" || { echo "error: $asset missing (scripts/art/split_art_sheets.py)" >&2; exit 1; }
done

DIST=viewer/dist
mkdir -p "$DIST"
rm -f "$DIST"/*.js "$DIST"/*.wasm "$DIST"/*.data "$DIST"/*.html "$DIST"/*.ttf
rm -rf replay-viewer/dist

"$NIM" c --hints:off -d:emscripten replay-viewer/halite_replay.nim
cp replay-viewer/dist/halite_replay.js replay-viewer/dist/halite_replay.wasm \
   replay-viewer/dist/halite_replay.data "$DIST"/
rm -rf replay-viewer/dist/nimcache
cp client/broadcast_core.js "$DIST"/broadcast_core.js
cp client/chrome_common.js "$DIST"/chrome_common.js
cp client/font.ttf "$DIST"/font.ttf
cp replay-viewer/static_replay.js "$DIST"/static_replay.js
cp replay-viewer/static_replay_worker.js "$DIST"/static_replay_worker.js
cp client/replay_broadcast.html "$DIST"/index.html

# Guard chain (ctf style): every file the page loads, the wiring between them,
# and the things that must NOT be there.
test -f "$DIST"/halite_replay.wasm
test -f "$DIST"/halite_replay.js
test -f "$DIST"/halite_replay.data
test -f "$DIST"/static_replay_worker.js
test -f "$DIST"/index.html
test -s "$DIST"/chrome_common.js
grep -q 'window.ChromeCommon' "$DIST"/chrome_common.js
grep -q 'chrome_common.js' "$DIST"/index.html
test -s "$DIST"/broadcast_core.js
grep -q 'window.BroadcastCore' "$DIST"/broadcast_core.js
grep -q 'static_replay.js' "$DIST"/index.html
grep -q 'static_replay_worker.js' "$DIST"/static_replay.js
grep -q "importScripts('./broadcast_core.js', './halite_replay.js')" "$DIST"/static_replay_worker.js
grep -q '_halite_load_replay' "$DIST"/halite_replay.js
grep -q '_halite_stage_ptr' "$DIST"/halite_replay.js
# The page must fetch the replay itself (?replay= / /replay-data) and never
# load the wasm runtime on the main thread.
grep -q "get('replay')" "$DIST"/index.html
! grep -q '<script src="./broadcast_core.js"></script>' "$DIST"/index.html
! grep -q '<script src="./halite_replay.js"></script>' "$DIST"/index.html
# Relative asset paths only (the bundle is served from S3 under
# /v2/coworlds/replays/static/<cow_id>/<sha>/, never from the Observatory's
# own host and never from the game pod).
! grep -Eq 'src="/[^/]' "$DIST"/index.html

ls -la "$DIST"
echo "build_viewer: OK"
