"""13. Viewer — the shipped page, the chrome pins and the built bundle.

The page and the bundle are static artefacts, so most of this is a read of the
files the browser will actually load. The one dynamic part runs the bundle's
own JS under node against the CI replay when a build is present; without a
built bundle (the sandbox has no emsdk) it is skipped and the ``wasm-viewer``
CI job is the gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "client" / "replay_broadcast.html"
CHROME = REPO / "client" / "chrome_common.js"
CORE = REPO / "client" / "broadcast_core.js"
STATIC_JS = REPO / "replay-viewer" / "static_replay.js"
WORKER_JS = REPO / "replay-viewer" / "static_replay_worker.js"
CONFIG_NIMS = REPO / "replay-viewer" / "config.nims"
NIM = REPO / "replay-viewer" / "halite_replay.nim"
CTF = Path("/workspace/starters/coworld-ctf")

PAGE_TEXT = PAGE.read_text()


# ------------------------------------------------------------- provenance
#: sha256 of coworld-ctf's own `client/chrome_common.js` and
#: `client/broadcast_core.js`, recorded from the read-only starter mount this
#: repo copied them from (`/workspace/starters/coworld-ctf`). The mount does
#: not exist on a GitHub runner, so without these digests the byte-for-byte
#: pin was verified only in the sandbox and CI enforced nothing.
CTF_CHROME_SHA256 = "7ace7287e0d19bf0fddb2362c55e4d76dfb44adcd4fbc8d1743b0557ced72f7c"
CTF_CORE_SHA256 = "172c4680129d608fd687cfd86436b675eef32c8652be6afe5f3189dd20c5aa9c"


@pytest.mark.parametrize(
    "path,digest",
    [("client/chrome_common.js", CTF_CHROME_SHA256),
     ("client/broadcast_core.js", CTF_CORE_SHA256)],
)
def test_the_chrome_files_hash_to_the_starter_digests(path, digest):
    """The byte-for-byte pin, enforced WHEREVER the tests run — including CI,
    where there is no starter mount. Unused ctf helpers stay in the file,
    unreferenced; deleting from a byte-for-byte copy is precisely what the pin
    forbids, and a one-character edit moves the digest."""
    actual = hashlib.sha256((REPO / path).read_bytes()).hexdigest()
    assert actual == digest, (
        f"{path} is no longer coworld-ctf's file byte for byte "
        f"(sha256 {actual}, want {digest})"
    )


def test_the_chrome_and_the_compositor_are_the_starter_files_byte_for_byte():
    """And when the starter mount IS present, compare the bytes themselves —
    which is also what proves the digests above were not copied from us."""
    if not CTF.is_dir():
        pytest.skip("the coworld-ctf mount is not present")
    assert CHROME.read_bytes() == (CTF / "client" / "chrome_common.js").read_bytes()
    assert CORE.read_bytes() == (CTF / "client" / "broadcast_core.js").read_bytes()
    assert hashlib.sha256(
        (CTF / "client" / "chrome_common.js").read_bytes()
    ).hexdigest() == CTF_CHROME_SHA256
    assert hashlib.sha256(
        (CTF / "client" / "broadcast_core.js").read_bytes()
    ).hexdigest() == CTF_CORE_SHA256


def test_the_bootstrap_and_the_link_flags_come_from_the_same_starter():
    """Splicing one starter's shell onto another's emscripten bootstrap
    deadlocks the viewer silently with every file present and every request 200
    (cogame-lantern, 2026-08-23)."""
    static = STATIC_JS.read_text()
    worker = WORKER_JS.read_text()
    nims = CONFIG_NIMS.read_text()
    assert "coworld-ctf" in static and "coworld-ctf" in worker
    # onRuntimeInitialized boot, NOT a MODULARIZE/EXPORT_NAME factory.
    assert "Module.onRuntimeInitialized" in worker
    flags = nims[nims.index('switch(\n  "passL"'):]
    assert "MODULARIZE" not in flags and "EXPORT_NAME" not in flags
    # Every exported symbol the worker calls must be exported by the linker.
    exported = re.search(r"EXPORTED_FUNCTIONS=([^\s]+)", nims).group(1).split(",")
    for symbol in re.findall(r"Module\.(_halite_[a-z_]+)", worker):
        assert symbol in exported, f"{symbol} is called but never exported"
    assert "_halite_load_replay" in exported and "_halite_frame" in exported


def test_the_three_documented_adaptations_and_no_others():
    static = STATIC_JS.read_text()
    worker = WORKER_JS.read_text()
    # (1) start() takes the replay bytes the page fetched.
    assert "function start(replayBytes)" in static
    assert "message.replayBytes" in worker
    # (2) nothing is re-simulated, so the sim mismatch tick is gone.
    assert "mismatch_tick" not in static and "mismatch_tick" not in worker
    assert "data-replay-mismatch-tick" not in static
    # (3) the exported symbols are renamed _halite_*.
    assert "_ctf_" not in worker and "_ctf_" not in static
    assert "importScripts('./broadcast_core.js', './halite_replay.js');" in worker
    # The failure marker on <html> is KEPT: without it a deadlocked bundle is
    # indistinguishable from a slow one.
    assert "data-replay-error" in static
    assert "data-replay-loaded" in static


def test_the_page_declares_its_provenance_and_its_removals():
    assert "HALITE additions to the inherited coworld-ctf chrome" in PAGE_TEXT
    for removed in ("#fpv", "#lockerroom", "#povBadge", "#viewpanel", "#minimap"):
        assert removed in PAGE_TEXT, f"the banner must name {removed} as removed"


# ----------------------------------------------------------------- the DOM
KEPT_IDS = [
    "chrome", "scorebug", "plates-l", "plates-r", "clock", "clock-time",
    "clock-caption", "stage", "viewport", "board", "status", "killfeed",
    "bannerlane", "grain", "lightpool", "endcard", "transport", "btn-restart",
    "btn-back", "btn-play", "btn-fwd", "btn-skip", "btn-end", "btn-loop",
    "btn-spoilers", "win-chip", "tick-clock", "speedchips", "scrub",
    "scrub-fill", "scrub-win", "scrub-head",
]
REMOVED_IDS = [
    "fpv", "fpv-canvas", "fpv-cap", "fpv-gear", "fpv-grip", "fpv-hp", "fpv-hud",
    "fpv-map", "fpv-map-canvas", "fpv-name", "lockerroom", "lk-art", "lk-bg",
    "lk-cap", "lk-sprites", "povBadge", "viewpanel", "zoombar", "zoom-in",
    "zoom-out", "zoom-read", "zoom-slider", "minimap", "minimap-canvas", "mmwarn",
]
#: chrome_common.js dereferences these unconditionally, and it is pinned
#: byte-for-byte, so they stay as hidden stubs (see the page's banner comment).
STUB_IDS = ["momentum", "lulls", "ffwd-chip", "ffwd-mini"]


@pytest.mark.parametrize("element", KEPT_IDS)
def test_the_inherited_chrome_elements_are_kept(element):
    assert f'id="{element}"' in PAGE_TEXT


@pytest.mark.parametrize("element", REMOVED_IDS)
def test_the_removed_elements_are_gone(element):
    assert f'id="{element}"' not in PAGE_TEXT


@pytest.mark.parametrize("element", STUB_IDS)
def test_the_chrome_common_stubs_exist_and_are_never_drawn(element):
    assert f'id="{element}"' in PAGE_TEXT
    assert element in re.search(
        r"#momentum, #lulls, #ffwd-chip, #ffwd-mini \{[^}]*display: none",
        PAGE_TEXT,
    ).group(0)


def test_every_id_chrome_common_dereferences_is_present():
    """chrome_common.js is byte-for-byte, so a missing node is a TypeError on
    the first frame."""
    for element in sorted(set(re.findall(r"\$\('([a-z0-9-]+)'\)", CHROME.read_text()))):
        assert f'id="{element}"' in PAGE_TEXT, f"chrome_common.js needs #{element}"


# ------------------------------------------------------------ transport rules
def test_relayout_owns_hudscale_topband_and_band_on_the_root():
    block = PAGE_TEXT[PAGE_TEXT.index("function relayout()"):]
    block = block[: block.index("window.addEventListener('resize'")]
    for name in ("--hudscale", "--topband", "--band"):
        assert f"root.style.setProperty('{name}'" in block, name
    assert "document.documentElement" in block


def test_the_board_is_fitted_between_the_two_bands():
    assert "top: var(--topband, 0px);" in PAGE_TEXT
    assert "height: calc(100% - var(--topband, 0px) - var(--band, 0px));" in PAGE_TEXT


def test_the_endcard_stops_at_the_transport_band_and_every_seek_dismisses_it():
    card = PAGE_TEXT[PAGE_TEXT.index("#endcard {"):]
    card = card[: card.index("}")]
    assert "bottom: var(--band, 0px)" in card
    seek = PAGE_TEXT[PAGE_TEXT.index("function seek(target)"):]
    seek = seek[: seek.index("function advance")]
    assert "showEndcard(false)" in seek, "every seek must dismiss the endcard"


def test_no_overlay_sits_in_the_transport_band():
    """Nothing the game block adds is positioned over #transport."""
    block = PAGE_TEXT[PAGE_TEXT.index("HALITE additions"):]
    assert "#transport" not in block.split("<script>")[0].replace(
        "the whole #transport", ""), "the appended CSS must not restyle the transport band"


# ------------------------------------------------------------ scrubber beats
def test_beats_are_labelled_clickable_buttons():
    builder = PAGE_TEXT[PAGE_TEXT.index("function haliteBeat("):]
    builder = builder[: builder.index("function placeBeats")]
    assert "createElement('button')" in builder
    assert "btn.title" in builder and "aria-label" in builder
    assert "addEventListener('click'" in builder and "seek(tick)" in builder


def test_every_beat_kind_events_py_emits_has_a_css_rule():
    from cogame_halite.events import BEAT_KINDS

    for kind, label in BEAT_KINDS.items():
        assert f".beat-marker.{kind}" in PAGE_TEXT, f"no CSS for beat kind {kind}"
        assert f"'{label}'" in PAGE_TEXT, f"beat kind {kind} has no label {label!r}"
    assert set(BEAT_KINDS) == {
        "convert", "collide", "yardraze", "eliminate", "lead", "guard"}
    assert "button.beat-marker" in PAGE_TEXT


def test_the_game_block_does_not_shadow_the_chrome_alias_list():
    """The chrome alias block declares markBeat with a hoisted `var`; a
    game-block `function markBeat` is silently swallowed by it and the scrubber
    ends up with unlabelled div markers that never seek (cogame-tandem,
    2026-08-23)."""
    aliases = re.findall(r"^\s*var ([A-Za-z_$][\w$]*) = C\.", PAGE_TEXT, flags=re.M)
    assert "markBeat" in aliases, "the chrome alias must be present"
    for name in aliases:
        redeclared = re.findall(rf"^\s*function {re.escape(name)}\s*\(", PAGE_TEXT, flags=re.M)
        assert not redeclared, f"the game block redeclares the chrome alias {name}"
    assert "function haliteBeat(" in PAGE_TEXT, "the game block's own builder"
    # And no alias is declared twice.
    assert len(aliases) == len(set(aliases)), "an alias is declared twice"


def test_the_plate_name_survives_a_360px_iframe():
    rule = re.search(r"\.plate-name \{[^}]*\}", PAGE_TEXT).group(0)
    assert "flex: 1 1 auto" in rule
    assert "min-width: 3.2em" in rule
    assert "#stage.tiny .plate .hal-sub" in PAGE_TEXT, "secondary labels hide under 640px"
    assert "stage.classList.toggle('tiny', w < 640)" in PAGE_TEXT


# ---------------------------------------------------------------- the boot
def test_the_page_fetches_the_replay_itself_and_never_loads_the_runtime_inline():
    assert 'script src="./chrome_common.js"' in PAGE_TEXT
    assert 'script src="./static_replay.js"' in PAGE_TEXT
    assert 'script src="./broadcast_core.js"' not in PAGE_TEXT, (
        "broadcast_core.js runs in the Worker (importScripts), never on the main thread"
    )
    assert 'script src="./halite_replay.js"' not in PAGE_TEXT
    assert "fetch(replayUrl()" in PAGE_TEXT
    assert "core.start(bytes)" in PAGE_TEXT


def test_ready_is_posted_from_the_data_replay_loaded_callback():
    """softmax.com sampled an unpainted shell when `ready` was posted on rAF
    timing at the call site (chorus 2026-08-24)."""
    block = PAGE_TEXT[PAGE_TEXT.index("new MutationObserver("):]
    block = block[: block.index("function replayUrl")]
    assert "data-replay-loaded" in block and "postReady()" in block
    assert "attributeFilter: ['data-replay-loaded']" in block


def test_only_relative_asset_paths():
    assert not re.search(r'src="/[^/]', PAGE_TEXT)
    assert not re.search(r'href="/[^/]', PAGE_TEXT)


# ------------------------------------------------------------- the renderer
def test_the_nim_renderer_draws_recorded_state_and_never_re_simulates():
    text = NIM.read_text()
    assert "cogame-halite-replay" in text
    assert "Nothing is re-simulated here" in text
    # The cargo-at-risk predicate is the ram rule's own.
    assert "other.cargo <= ship.cargo" in text
    for asset in ("data/art/seabed.png", "data/art/halite_crystals.png",
                  "data/art/hull_", "data/art/yard_"):
        assert asset in text


@pytest.mark.parametrize(
    "asset",
    ["seabed.png", "halite_crystals.png"]
    + [f"hull_{s}.png" for s in ("alpha", "bravo", "charlie", "delta")]
    + [f"yard_{s}.png" for s in ("alpha", "bravo", "charlie", "delta")],
)
def test_every_art_asset_is_committed(asset):
    path = REPO / "data" / "art" / asset
    assert path.is_file() and path.stat().st_size > 0


def test_the_art_sources_and_the_split_script_are_committed():
    for sheet in ("hulls_sheet.png", "yards_sheet.png", "halite_crystals.png",
                  "seabed_tile.png"):
        assert (REPO / "scripts" / "art" / "source" / sheet).is_file()
    assert (REPO / "scripts" / "art" / "split_art_sheets.py").is_file()


def test_the_build_hook_asserts_every_file_the_page_references():
    hook = (REPO / "tools" / "build_replay_viewer.sh").read_text()
    for name in ("index.html", "chrome_common.js", "broadcast_core.js",
                 "static_replay.js", "static_replay_worker.js", "halite_replay.js",
                 "halite_replay.wasm", "halite_replay.data", "font.ttf"):
        assert name in hook, f"the hook does not assert {name}"
    assert 'mkdir -p "$(dirname "${requested_output}")"' in hook, (
        "the ecos 2026-08-23 fix: mkdir the output parent BEFORE the containment check"
    )


# ------------------------------------------------------------- the fixtures
def test_the_renderer_fixture_loads_the_shipped_page_and_full_cap_notes():
    text = (REPO / "tools" / "ci" / "renderer_fixture.html").read_text()
    assert "./index.html?shim=1" in text, "it must load the SHIPPED page"
    assert "var CAP = 140" in text
    assert "SIZES = [[1240, 700], [900, 560], [360, 640]]" in text, (
        "three canvas sizes, each in its OWN fixed iframe -- resizing one frame "
        "races the child's layout lifecycle"
    )
    assert "fillText" in text, "the DOM runs are transcribed to canvas"


def test_the_narrow_fixture_is_360x640():
    text = (REPO / "tools" / "ci" / "narrow_fixture.html").read_text()
    assert "width: 360px; height: 640px" in text
    assert "./index.html?replay=" in text


def test_ci_runs_all_three_viewer_passes():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "--strict-text-bounds" in ci
    assert "--soak 12" in ci
    assert "narrow_fixture.html" in ci
    assert "renderer_fixture.html" in ci
    assert ci.count("viewer_smoke.mjs \\") >= 3


# ---------------------------------------------------- the built bundle (node)
BUNDLE = REPO / "dist" / "static-replay-viewer"


@pytest.mark.skipif(
    not (BUNDLE / "index.html").is_file() or shutil.which("node") is None,
    reason="no built bundle here (the sandbox has no emsdk); the wasm-viewer CI job is the gate",
)
def test_the_built_bundle_parses_the_ci_replay_under_node(tmp_path):
    replay = next((REPO / "dist" / "smoke").glob("*.replay"), None)
    if replay is None:
        pytest.skip("no CI replay in dist/smoke")
    script = tmp_path / "check.mjs"
    script.write_text(
        "import { readFileSync } from 'node:fs';\n"
        f"const doc = JSON.parse(readFileSync({str(replay)!r}, 'utf8'));\n"
        "if (doc.format !== 'cogame-halite-replay') throw new Error('bad format');\n"
        "if (!doc.turns.length) throw new Error('no turns');\n"
        f"const page = readFileSync({str(BUNDLE / 'index.html')!r}, 'utf8');\n"
        "for (const f of ['chrome_common.js','static_replay.js'])\n"
        "  if (!page.includes(f)) throw new Error('index.html does not load ' + f);\n"
        "console.log(JSON.stringify({turns: doc.turns.length}));\n"
    )
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["turns"] > 0


# ------------------------------------------- the page's own logic, under node
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_shipped_page_boots_plays_and_draws_under_a_dom_stub(tmp_path):
    """The sandbox has no browser and CI's wasm-viewer job is the real gate,
    but a stub run catches what that gate cannot report cheaply: a throw in the
    boot path, a scorebug that never builds, a scrubber with no beats, or
    playback that does not advance. Only the wasm half is stubbed; the chrome
    and the page's own block are the shipped code."""
    import asyncio

    from conftest import FakeLink, make_config
    from cogame_halite.engine import Engine

    async def nosleep(_seconds):
        return None

    async def episode():
        engine = Engine(
            make_config(episode_steps=120),
            [FakeLink(i, baseline=("tidewalker", "corsair")[i % 2],
                      note="squeezing BRAVO off the north cluster \U0001F6A2")
             for i in range(4)],
            sleep=nosleep,
        )
        return await engine.run()

    outcome = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(episode())
    fixture = tmp_path / "page.replay"
    fixture.write_bytes(outcome.replay.to_bytes())

    result = subprocess.run(
        ["node", str(REPO / "tests" / "page_dom_harness.mjs"), str(REPO), str(fixture)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"the shipped page failed under the DOM stub:\n{result.stdout}\n{result.stderr}"
    )
    report = json.loads(result.stdout)
    assert report["plates"] == 4
    assert report["beats"] >= 5
    assert report["feed_lines"] > 0
    assert report["startedBytes"] == fixture.stat().st_size


def test_ci_gates_the_renderer_fixture_on_a_non_vacuous_text_count():
    """viewer_smoke.mjs accepts the bridge `ready` OR data-replay-loaded,
    whichever comes first; the shipped page inside the fixture's iframe posts
    `ready` within ~300 ms, before the fixture has transcribed anything. The
    soak keeps the page alive for the three passes and this assertion is what
    makes the text gate mean something (cogchemists 2026-08-24)."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "--soak 8" in ci
    assert 'text["total"] < 12' in ci
    assert 'text["never_inside"]' in ci
