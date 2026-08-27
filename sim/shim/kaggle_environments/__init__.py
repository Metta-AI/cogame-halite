"""Package shim for the assembled Halite tree.

`sim/assemble.py` copies this file (and the two empty ``__init__.py`` files
beside it) into ``build/khalite/kaggle_environments/`` next to the byte-pristine
vendored modules. Upstream's own ``kaggle_environments/__init__.py`` is a
heavyweight module (agent registry, HTTP api, notebook renderers) that the two
files we vendor do not need; all they need from the package is:

  * ``kaggle_environments.helpers`` importable (``envs/halite/helpers.py`` does
    ``import kaggle_environments.helpers``), and
  * a ``kaggle_environments.utils`` exposing ``structify`` (``envs/halite/
    halite.py`` does ``from kaggle_environments import utils`` at module level).

Nothing here touches a vendored byte — see ``vendor/PATCHES.md``.
"""

import sys
import types
from typing import Any

from . import helpers  # noqa: F401  (byte-pristine vendored module)


class Struct(dict):
    """Upstream ``kaggle_environments.utils.Struct``."""

    def __init__(self, **entries: Any) -> None:
        entries = {k: v for k, v in entries.items() if k != "items"}
        dict.__init__(self, entries)
        self.__dict__.update(entries)

    def __setattr__(self, attr: str, value: Any) -> None:
        self.__dict__[attr] = value
        self[attr] = value


def structify(o: Any) -> Any:
    """Upstream ``kaggle_environments.utils.structify``."""
    if isinstance(o, list):
        return [structify(o[i]) for i in range(len(o))]
    if isinstance(o, dict):
        return Struct(**{k: structify(v) for k, v in o.items()})
    return o


utils = types.ModuleType("kaggle_environments.utils")
utils.Struct = Struct
utils.structify = structify
sys.modules["kaggle_environments.utils"] = utils
