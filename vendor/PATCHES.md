# Patches applied to `vendor/upstream/`

## Zero patches.

Not "few". **None.** `sim/assemble.py` copies `vendor/upstream/**` into
`build/khalite/` byte for byte and adds three package `__init__.py` files from
`sim/shim/`. `tests/test_vendor.py` proves it: every non-shim file in the
assembled tree is byte-compared against its `vendor/upstream/` original, and
every sha256 is re-derived from `vendor/UPSTREAM.md`.

## Why a shim exists at all, and what it does

`vendor/upstream/kaggle_environments/envs/halite/helpers.py` opens with

```python
import kaggle_environments.helpers
from kaggle_environments.helpers import Point, group_by
```

and `.../halite/halite.py` adds

```python
from kaggle_environments import utils
```

so the two modules only import if they sit inside a package named
`kaggle_environments`. Upstream's own `kaggle_environments/__init__.py` is a
heavyweight module (agent registry, HTTP api, jupyter renderers, a `main`
entrypoint) that drags in dependencies this coworld has no business shipping,
and vendoring it would mean vendoring most of the distribution.

So the assembled tree gets **package `__init__.py` files that upstream would
otherwise supply**, and nothing else:

| file | content |
|---|---|
| `kaggle_environments/__init__.py` | re-exports the vendored `.helpers`, and registers a `kaggle_environments.utils` module carrying `Struct` + `structify` |
| `kaggle_environments/envs/__init__.py` | empty |
| `kaggle_environments/envs/halite/__init__.py` | empty |

`Struct`/`structify` are a 12-line copy of upstream's `utils.py` definitions of
the same names, present **only to satisfy `halite.py`'s module-level import**:
the production sim never calls `interpreter()` (the only consumer of
`structify` in the halite env), because our engine owns seat statuses — see
"the elimination block", below. They are not game rules and they touch no
vendored byte.

## The two transcriptions, named

`server/cogame_halite/sim.py` imports and calls the vendored
`Board(obs, config, actions).next()` for **every** rule of turn resolution. Two
upstream code paths are reached differently, both named here with their
citation, both covered by the fidelity gate (`tests/test_fidelity.py`):

1. **`populate_board` adapter** (~20 lines, `sim.py::_populate`).
   `kaggle_environments/envs/halite/halite.py::populate_board(state, env)` takes
   the framework's `state`/`env` duck types (`env.configuration.randomSeed`,
   `state[0].observation`, `state[0].reward`). The adapter builds those objects;
   **the function body it calls is upstream's**, unmodified, imported from the
   assembled tree.
2. **The elimination + last-fleet block** (12 lines, `sim.py::_eliminate`), a
   transcription of `halite.py::interpreter()` lines 194–209. Our engine owns
   seat status (`ACTIVE`/`DONE`) and the episode end rule, so the framework's
   `interpreter` cannot be called; its constants (`config.spawnCost`,
   `board.step - configuration.episode_steps - 1`) are read from the **vendored
   `halite.json`** and from the vendored `Configuration`, never re-typed.

`tests/test_sim.py` asserts every numbered rule of the design note's
§"Turn resolution" against these paths, and `tests/test_fidelity.py` runs
8 seeds × 399 turns of exact-equality differential comparison against a real
`kaggle-environments==1.32.7` `make("halite")` env, which covers both
transcriptions end to end.
