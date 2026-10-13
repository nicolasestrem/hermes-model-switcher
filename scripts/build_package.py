#!/usr/bin/env python3
"""Assemble the releasable plugin package.

Produces a self-contained plugin directory in the OFFICIAL dashboard-plugin
layout (website/docs/user-guide/features/extending-the-dashboard.md):

    <out>/hermes-model-switcher/
    ├── plugin.yaml              CLI/gateway manifest (informational; no hooks yet)
    ├── LICENSE
    ├── README.md
    ├── python/                  Python modules the backend imports
    │   ├── hermes_adapter.py
    │   ├── route_inventory.py
    │   └── model_switcher/      package (engine, journal, presets, registry)
    └── dashboard/
        ├── manifest.json        Dashboard manifest (entry dist/index.js, api plugin_api.py)
        ├── dist/index.js        pre-built IIFE bundle (host React via SDK shims)
        └── plugin_api.py        FastAPI routes mounted at /api/plugins/<name>/

Usage: python scripts/build_package.py [--out DIR] (default: dist-package/)
Exit codes: 0 ok, 1 missing inputs.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME = "hermes-model-switcher"

COPY_FILES = ["plugin.yaml", "LICENSE", "README.md"]
PY_MODULES = ["hermes_adapter.py", "route_inventory.py"]
PY_PACKAGE = "model_switcher"
DASHBOARD = ["manifest.json", "plugin_api.py"]
BUNDLE = "dist/index.js"  # built by `npm run build`


def fail(msg: str) -> None:
    print(f"build_package: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "dist-package"))
    args = ap.parse_args()
    out = Path(args.out) / NAME
    if out.exists():
        shutil.rmtree(out)

    bundle = REPO / "dashboard" / BUNDLE
    if not bundle.is_file():
        fail(f"missing {bundle} — run `npm run build` first")

    dash = out / "dashboard" / "dist"
    dash.mkdir(parents=True)
    for f in COPY_FILES:
        if not (REPO / f).is_file():
            fail(f"missing {f}")
        shutil.copy2(REPO / f, out / f)
    pydir = out / "python"
    pydir.mkdir()
    for f in PY_MODULES:
        if not (REPO / f).is_file():
            fail(f"missing {f}")
        shutil.copy2(REPO / f, pydir / f)
    shutil.copytree(REPO / "src" / PY_PACKAGE, pydir / PY_PACKAGE,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for f in DASHBOARD:
        shutil.copy2(REPO / "dashboard" / f, out / "dashboard" / f)
    (dash / "index.js").write_bytes(bundle.read_bytes())
    print(f"package assembled at {out}")


if __name__ == "__main__":
    main()
