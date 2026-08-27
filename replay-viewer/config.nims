import std/[os, strformat, strutils]

# coworld-ctf replay-viewer/config.nims, with the outputs renamed
# halite_replay.{js,wasm,data} and the exported symbols renamed _halite_*.
# The emscripten link block and replay-viewer/static_replay*.js are a MATCHED
# PAIR and both come from coworld-ctf: splicing one starter's shell onto
# another's bootstrap (MODULARIZE/EXPORT_NAME vs an onRuntimeInitialized boot)
# deadlocks the viewer silently with every file present and every request 200
# (cogame-lantern, 2026-08-23).

let rootDir = currentSourcePath().parentDir().parentDir()
let distDir = rootDir / "replay-viewer" / "dist"

if not dirExists(distDir):
  mkDir(distDir)

switch("nimcache", distDir / "nimcache")
switch("threads", "off")
--os:linux
--cpu:wasm32
--cc:clang
--clang.exe:emcc
--clang.linkerexe:emcc
--clang.cpp.exe:emcc
--clang.cpp.linkerexe:emcc
--mm:arc
--exceptions:goto
--define:noSignalHandler
--define:release
# Route every allocation through emscripten's malloc (the standard Nim
# emscripten setup). With Nim's bundled allocator a bad free silently poisons
# the freelists; dlmalloc traps loudly instead.
--define:useMalloc

# ENVIRONMENT includes worker because the shipped static bundle owns the WASM
# runtime in a Dedicated Worker, and node so CI can smoke-run that EXACT
# emitted module.
# ABORTING_MALLOC matters: with -d:useMalloc Nim never checks malloc for nil,
# and wasm32 has no memory protection, so a failed allocation would otherwise
# write the seq header through the nil pointer into address 0 — silently
# corrupting the module's own globals. Aborting keeps linear memory intact, and
# the page reads halite_stage_ptr/len afterwards to report what the runtime was
# doing.
switch(
  "passL",
  (&"""
  -o {distDir / "halite_replay.js"}
  --preload-file {rootDir / "data"}@data
  -O2
  -s ALLOW_MEMORY_GROWTH
  -s ABORTING_MALLOC=1
  -s FILESYSTEM=1
  -s ENVIRONMENT=web,worker,node
  -s EXPORTED_RUNTIME_METHODS=HEAPU8
  -s EXPORTED_FUNCTIONS=_main,_malloc,_free,_halite_load_replay,_halite_frame,_halite_input,_halite_packet_ptr,_halite_packet_len,_halite_error_ptr,_halite_error_len,_halite_stage_ptr,_halite_stage_len
  """).replace("\n", " ")
)
