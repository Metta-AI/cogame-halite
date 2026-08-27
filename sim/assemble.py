"""Assemble the importable Halite tree from the pristine vendor + the shims.

    build/khalite/kaggle_environments/**  =  vendor/upstream/**  +  sim/shim/**

`vendor/upstream/` is byte-pristine (`AGENTS.md` rule 1) and cannot carry the
package `__init__.py` files upstream's own distribution supplies, so they come
from `sim/shim/` instead. Copying is a plain byte copy — no rewriting, no
patching, no codegen. `tests/test_vendor.py` byte-compares every non-shim file
in the assembled tree against its vendor original.

Importable either way:

    python sim/assemble.py [--out build/khalite] [--check]
    from sim.assemble import assemble, assembled_root
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ROOT = REPO_ROOT / "vendor" / "upstream"
SHIM_ROOT = REPO_ROOT / "sim" / "shim"
DEFAULT_OUT = REPO_ROOT / "build" / "khalite"

#: Files that come from ``sim/shim/`` rather than ``vendor/upstream/``. Every
#: other file in the assembled tree must be byte-identical to its vendor
#: original; ``tests/test_vendor.py`` asserts exactly that.
SHIM_FILES = (
    "kaggle_environments/__init__.py",
    "kaggle_environments/envs/__init__.py",
    "kaggle_environments/envs/halite/__init__.py",
)

VENDOR_FILES = (
    "kaggle_environments/helpers.py",
    "kaggle_environments/envs/halite/helpers.py",
    "kaggle_environments/envs/halite/halite.py",
    "kaggle_environments/envs/halite/halite.json",
)


def assemble(out: Path = DEFAULT_OUT, *, force: bool = False) -> Path:
    """Copy vendor + shim into ``out`` and return it. Idempotent."""
    out = Path(out)
    if force and out.exists():
        shutil.rmtree(out)
    for rel in VENDOR_FILES:
        src = VENDOR_ROOT / rel
        if not src.is_file():
            raise FileNotFoundError(f"vendored file missing: {src}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
    for rel in SHIM_FILES:
        src = SHIM_ROOT / rel
        if not src.is_file():
            raise FileNotFoundError(f"shim file missing: {src}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
    return out


def assembled_root(out: Path = DEFAULT_OUT) -> Path:
    """Assemble if needed and put the tree on ``sys.path``.

    Callers then ``import kaggle_environments.envs.halite.helpers`` and get the
    vendored code. Never installs the real PyPI package — the production sim
    must run against `vendor/upstream/` and nothing else. (The CI-only
    ``fidelity`` group *does* install the real package; ``test_fidelity.py``
    imports it under a separate interpreter path guard.)
    """
    root = assemble(out)
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="rebuild from scratch")
    parser.add_argument(
        "--check",
        action="store_true",
        help="after assembling, verify every non-shim file byte-equals vendor",
    )
    args = parser.parse_args(argv)
    out = assemble(args.out, force=args.force)
    if args.check:
        for rel in VENDOR_FILES:
            if (out / rel).read_bytes() != (VENDOR_ROOT / rel).read_bytes():
                print(f"assembled tree diverges from vendor: {rel}", file=sys.stderr)
                return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
