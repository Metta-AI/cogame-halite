# Vendored upstream — `kaggle-environments` (Halite IV)

`vendor/upstream/` is **byte-pristine**. Nothing under it is ever edited (see
`AGENTS.md` rule 1). The production sim *imports* these files through the
assembled tree that `sim/assemble.py` builds; it does not transcribe them.

## Source

| | |
|---|---|
| Project | [`github.com/Kaggle/kaggle-environments`](https://github.com/Kaggle/kaggle-environments) |
| Licence | Apache-2.0 — `vendor/LICENSE-kaggle-environments` |
| Release | PyPI `kaggle-environments==1.32.7` |
| Commit | `28b6d8af3ce73926b3d0fda1410c1ddd8384ab8c` (2026-08-14, the commit that set `version = "1.32.7"` in `pyproject.toml`) |
| Env | `kaggle_environments/envs/halite` — spec `"version": "1.2.1"`, `"title": "Halite 4"` |

The four files below are byte-identical between that commit and the published
`kaggle_environments-1.32.7-py3-none-any.whl`, which is what
`tests/test_fidelity.py` installs from the CI-only `fidelity` dependency group
to run the differential gate.

## Per-file sha256

| file (under `vendor/upstream/`) | sha256 |
|---|---|
| `kaggle_environments/helpers.py` | `131e30de2be082120d3ccf9f808012bed8c9ec1fbaed614b300475b3a1b3dbc0` |
| `kaggle_environments/envs/halite/helpers.py` | `44f1ddf90d9de459c7a1472fc840a6fe425219484bf632a1d569b9814c9c5224` |
| `kaggle_environments/envs/halite/halite.py` | `358c94fb511dc45526bdb00d9f8549b8926dc8c59885962f266308887fcb0a2d` |
| `kaggle_environments/envs/halite/halite.json` | `29f97303234847a80147a23d7ab25210211bd55dd01b9a667a8c869a365cc749` |

`tests/test_vendor.py` re-derives every sha256 in that table from this file and
fails on the first mismatch, so a re-vendor that changes a byte cannot land
silently.

## Toolchain pins (the wasm replay viewer)

The sim is Python, so nothing here compiles. The viewer does:

| tool | pin | where |
|---|---|---|
| emscripten (`emscripten/emsdk`) | `4.0.15` | `Dockerfile` (`wasm-builder`) |
| nimby | `0.1.27` | `Dockerfile` (`wasm-builder`) |
| Nim | `2.2.4` | `Dockerfile` (`wasm-builder`) |
| Nim packages | `nimby.lock` | `nimby --global sync nimby.lock` |

Bump these in the `Dockerfile` and here together.

## Why Python needs no wasm

cogame-moba compiles C to wasm because C's `rand()` and float behaviour differ
per libc. Halite is Python: the only randomness is CPython's Mersenne Twister
(`random.seed`/`randint`) and numpy's **legacy** `RandomState`
(`np.random.seed` + `gumbel`/`binomial`) — both contractually stable across
versions and platforms — and the arithmetic is IEEE-754 doubles plus
`round(x, 3)`. The portable artefact is the source itself, so the port imports
it (`vendor/PATCHES.md`).
