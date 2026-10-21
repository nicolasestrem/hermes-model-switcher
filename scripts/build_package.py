#!/usr/bin/env python3
"""Assemble the releasable plugin package.

Produces a self-contained plugin directory in the OFFICIAL dashboard-plugin
layout (website/docs/user-guide/features/extending-the-dashboard.md):

    <out>/hermes-model-switcher/
    ├── plugin.yaml              CLI/gateway manifest (informational; no hooks yet)
    ├── __init__.py              entry point Hermes requires beside plugin.yaml
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
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME = "hermes-model-switcher"

# ``__init__.py`` is not optional: a plugin.yaml with no __init__.py,
# desktop/plugin.js or plugin.json beside it is rejected by
# `hermes plugins validate`, and the agent loader records a load failure
# ("No __init__.py in <dir>") for it at every startup.
COPY_FILES = ["plugin.yaml", "__init__.py", "LICENSE", "README.md", "CHANGELOG.md", "DESIGN.md", "CONTRIBUTING.md"]
PY_MODULES = ["hermes_adapter.py", "route_inventory.py"]
PY_PACKAGE = "model_switcher"
DASHBOARD = ["manifest.json", "plugin_api.py", "model_catalog.py"]
BUNDLE = "dist/index.js"  # built by `npm run build`
STYLES = "dist/style.css"
# Explicit publication boundary: local/ignored documentation is never shipped.
DOC_FILES = [
    "usability-polish.md", "technical-reference.md", "privacy-audit.md",
    "release-readiness.md", "verification.md", "backlog.md", "catalog-submission.md", "images/README.md",
    "images/catalog-captures.json",
    "images/model-switcher-0.3.0-dashboard.png",
    "images/model-switcher-0.3.0-preview.png",
    "images/model-switcher-0.3.0-operation.png",
    "images/model-switcher-0.3.0-catalog.png",
    "images/model-switcher-0.3.0-light.png",
    "images/model-switcher-0.3.0-narrow.png",
    "images/model-switcher-dark-1440.png", "images/model-switcher-light-1440.png",
    "images/model-switcher-dark-375.png",
    "images/model-switcher-catalog-1440.png", "images/model-switcher-catalog-375.png",
    "images/model-switcher-real-dashboard.png", "images/model-switcher-real-preview.png",
    "images/model-switcher-real-operation.png",
]


def fail(msg: str) -> None:
    print(f"build_package: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "dist-package"))
    ap.add_argument("--replace", action="store_true",
                    help="replace an existing assembled package directory")
    args = ap.parse_args()
    requested = Path(args.out).absolute() / NAME
    if requested.is_symlink():
        fail("package output itself must not be a symlink")
    # Canonicalize parents for platforms where temporary/home directories
    # are aliases, then apply containment guards to the actual destination.
    out = requested.parent.resolve() / NAME
    if out == REPO or out in REPO.parents:
        fail("output must not replace the source checkout or its ancestors")
    # Refuse ANY destination inside the checkout, not just the recursively
    # copied trees: rmtree/rename must never touch tracked sources. The single
    # exception is the ignored, disposable default build dir (dist-package/).
    if REPO in out.parents and (REPO / "dist-package") not in out.parents:
        fail("output must not be inside the checkout; the only permitted "
             "in-checkout destination is the ignored dist-package/ build dir")
    if out.exists() and not args.replace:
        fail("output already exists; use --replace to replace it explicitly")
    inputs = [REPO / f for f in COPY_FILES + PY_MODULES + ["scripts/privacy_guard.py"]]
    inputs += [REPO / "dashboard" / f for f in DASHBOARD + [BUNDLE, STYLES]]
    inputs += [REPO / "docs" / f for f in DOC_FILES]
    for source in inputs:
        if not source.is_file():
            fail(f"missing {source.name}")
    if not (REPO / "src" / PY_PACKAGE).is_dir():
        fail("missing Python package source")
    if not (REPO / "docs").is_dir():
        fail("missing documentation")

    # copytree/copy2 follow links by default. Reject links in both the
    # recursively copied trees and every input's parent chain before replacing
    # any existing package, so external/private files cannot enter the bundle.
    recursive_inputs = [REPO / "docs", REPO / "src" / PY_PACKAGE]
    for source in inputs + recursive_inputs:
        for candidate in (source, *source.parents):
            if candidate == REPO.parent:
                break
            if candidate.is_symlink():
                fail("package inputs must not contain symlinks")
    for source in recursive_inputs:
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                fail("package inputs must not contain symlinks")

    bundle = REPO / "dashboard" / BUNDLE
    if not bundle.is_file():
        fail(f"missing {bundle} — run `npm run build` first")

    stylesheet = REPO / "dashboard" / STYLES
    if not stylesheet.is_file():
        fail(f"missing {stylesheet} — run `npm run build` first")

    # Build into a staging directory NEXT TO the final destination, then swap
    # with a same-filesystem rename: an existing package is only destroyed
    # after its replacement is fully assembled, and a mid-build failure
    # leaves the previous output intact.
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{NAME}.staging.", dir=str(out.parent)))
    try:
        build(staging / NAME, bundle, stylesheet)
        os.chmod(staging / NAME, 0o755)  # mkdtemp is 0700; keep the old default
        previous = None
        if out.exists():
            previous = Path(tempfile.mkdtemp(prefix=f".{NAME}.replaced.",
                                             dir=str(out.parent)))
            out.rename(previous / NAME)
        (staging / NAME).rename(out)
        if previous is not None:
            shutil.rmtree(previous)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"package assembled at {out}")


def build(out: Path, bundle: Path, stylesheet: Path) -> None:
    """Assemble the package tree at *out* (a fresh empty directory)."""
    dash = out / "dashboard" / "dist"
    dash.mkdir(parents=True)
    for f in COPY_FILES:
        if not (REPO / f).is_file():
            fail(f"missing {f}")
        shutil.copy2(REPO / f, out / f)
    for f in DOC_FILES:
        destination = out / "docs" / f
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "docs" / f, destination)
    # The README links to this contributor tool; ship it with the docs.
    (out / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "privacy_guard.py", out / "scripts" / "privacy_guard.py")
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
    (dash / "style.css").write_bytes(stylesheet.read_bytes())


if __name__ == "__main__":
    main()
