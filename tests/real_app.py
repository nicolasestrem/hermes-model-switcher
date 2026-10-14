"""Real FastAPI app for the Playwright harness: the ACTUAL plugin backend.

Serves dashboard/plugin_api.py's router (the real apply/undo engine over the
real Hermes canonical save seam) against a THROWAWAY lab Hermes home under
/tmp — never the live profile. Providers are mocked only at the credential
level (deterministic fake .env credentials); every Apply/Undo performs real
file saves and real journal writes.

Run with uvicorn:  HERMES_HOME=<lab> python -m uvicorn real_app:app --port 8932
Seed a lab home:   python real_app.py --seed /tmp/some-dir
Print seed data:   python real_app.py --print-profiles
No inference is ever called: /probe is blocked by design and no provider
network client is constructed.

The seed CLI is fail-closed: ``--seed`` refuses any target that the real Hermes
runtime resolver does not treat as its own root (a target nested under the
native ``~/.hermes`` resolves back to the live home), and refuses the native
home itself — checked BEFORE the first write. ``--print-profiles`` is a pure
data dump that never imports the router or needs a lab home.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES_SRC = Path(os.environ.get(
    "HERMES_MODEL_SWITCHER_HERMES_REPO", Path.home() / ".hermes" / "hermes-agent"))
for p in (str(REPO), str(REPO / "src"), str(HERMES_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

PROFILES = {
    "default": (
        "model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n"
        "providers:\n"
        "  zhipu:\n    base_url: http://127.0.0.1:8080/v1\n"
    ),
    "research": (
        "model:\n  provider: openai\n  default: gpt-5.1\n"
        "providers:\n"
        "  zhipu:\n    base_url: http://127.0.0.1:8080/v1\n"
    ),
}
# Deterministic mock credentials (lab-only, never a real provider key; no
# 'sk-' prefix so leak assertions can look for real key shapes safely).
FAKE_CREDENTIAL = "LAB-LOCAL-FAKE-CREDENTIAL-0000000001"

# Stable refusal message for a seed target outside an isolated Hermes root.
# Tests assert on it; do not change it without updating them.
SEED_ISOLATION_MESSAGE = "seed target is not an isolated Hermes home"


def seed_home(home: Path) -> Path:
    home = assert_seed_target_isolated(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(PROFILES["default"])
    # the "default" profile's credential marker lives at the home root
    (home / ".env").write_text(f"OPENAI_API_KEY={FAKE_CREDENTIAL}\n")
    names = [n for n in PROFILES if n != "default"]
    for name in names:
        pdir = home / "profiles" / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "config.yaml").write_text(PROFILES[name])
        (pdir / ".env").write_text(f"OPENAI_API_KEY={FAKE_CREDENTIAL}\n")
    # The plugin's state dir lives INSIDE the home but OUTSIDE the plugin
    # checkout (dashboard/plugin_api.py: _state_dir_for) — mirror it here so
    # the seeded preset is found by the real backend.
    state = home / "state" / "hermes-model-switcher"
    state.mkdir(parents=True, exist_ok=True)
    # Seed the reviewed "Fast lane" preset the same way the UI would save it:
    # the reviewed change intent (with slot identity) + slot-bound targets.
    ui_changes = [
        {"profileId": "default", "slotId": "main", "action": "set",
         "from": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
         "to": {"provider": "zhipu", "model": "glm-5.3-flash"}},
        {"profileId": "research", "slotId": "main", "action": "set",
         "from": {"provider": "openai", "model": "gpt-5.1"},
         "to": {"provider": "zhipu", "model": "glm-5.3-flash"}},
    ]
    (state / "presets.json").write_text(json.dumps([{
        "preset_id": "p-fast",
        "name": "Fast lane",
        "approved_classes": ["subscription_included", "metered"],
        "allow_unknown": False,
        "reviewed_profile_ids": ["default", "research"],
        "ui_changes": ui_changes,
        "targets": [
            {"profile": "default", "slot_id": "main", "store_kind": "canonical_config",
             "path": str(home / "config.yaml"),
             "changes": {"model.provider": "zhipu", "model.default": "glm-5.3-flash"},
             "cost_class": "metered"},
            {"profile": "research", "slot_id": "main", "store_kind": "canonical_config",
             "path": str(home / "profiles" / "research" / "config.yaml"),
             "changes": {"model.provider": "zhipu", "model.default": "glm-5.3-flash"},
             "cost_class": "metered"},
        ],
    }]))
    return home


def assert_seed_target_isolated(target: Path) -> Path:
    """Fail closed unless *target* is provably its own Hermes root.

    Evaluated with the REAL runtime resolver against the REQUESTED target, never
    against the inherited ``HERMES_HOME``: on a live host that env var points at
    the live profile, so trusting it would let ``--seed <native-home>/sub``
    write into the live tree. ``get_default_hermes_root`` resolves any home
    nested under the native ``~/.hermes`` back to that native root, so a target
    "under" ~/.hermes is not isolated; the native root itself is the live store
    and is refused outright. No caller writes anything until this returns.
    """
    from hermes_constants import get_default_hermes_root
    resolved = Path(target).expanduser().resolve(strict=False)
    native = get_default_hermes_root(home="").resolve(strict=False)
    root = get_default_hermes_root(home=str(resolved)).resolve(strict=False)
    if root != resolved:
        raise RuntimeError(
            f"{SEED_ISOLATION_MESSAGE}: {resolved} resolves to {root}")
    if resolved == native or native.is_relative_to(resolved):
        raise RuntimeError(
            f"{SEED_ISOLATION_MESSAGE}: {resolved} is the native Hermes home")
    # External/custom runtime roots can be live too. Never overwrite existing
    # profile data just because the resolver treats the target as its own root.
    for name in ("config.yaml", ".env", "profiles", "state"):
        candidate = resolved / name
        if candidate.exists() or candidate.is_symlink():
            raise RuntimeError(
                f"{SEED_ISOLATION_MESSAGE}: target contains existing profile data")
    return resolved


def create_lab_home() -> Path:
    home = assert_seed_target_isolated(
        Path(tempfile.mkdtemp(prefix="msw-e2e-", dir="/tmp")))
    seed_home(home)
    return home


# -- CLI (runs BEFORE any router import; see the ``__main__`` guard below) ---
def _cli() -> None:
    """Seed a lab home, or dump the synthetic PROFILES, then exit.

    Defined and dispatched before the FastAPI/plugin_api import so that
    ``--print-profiles`` is a pure data dump (no router construction, no lab
    home required) and ``--seed`` clears the isolation boundary before its
    first write.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", help="seed a lab home at this path (or 'auto')")
    ap.add_argument("--home-file", default=str(REPO / "tests" / "real-home.json"))
    ap.add_argument("--print-profiles", action="store_true",
                    help="print the seed documents as JSON and exit")
    args = ap.parse_args()
    if args.print_profiles:
        print(json.dumps(PROFILES))
        raise SystemExit(0)
    if args.seed:
        if args.seed == "auto":
            home = create_lab_home()
        else:
            home = seed_home(assert_seed_target_isolated(Path(args.seed)))
        Path(args.home_file).write_text(json.dumps({"home": str(home)}))
        print(home)
        raise SystemExit(0)
    ap.print_usage(sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    _cli()

# -- app (module-level; uvicorn imports it with HERMES_HOME in the env) ------
# Check the real runtime resolver before importing/mounting the API. A lab
# beneath ~/.hermes is not isolated just because HERMES_HOME points there.
def assert_lab_isolation() -> None:
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    declared = os.environ.get("HERMES_HOME")
    if not declared:
        raise RuntimeError("lab profile discovery escaped declared home")
    root = Path(declared).resolve()
    if get_profile_dir("default").resolve() != root:
        raise RuntimeError("lab profile discovery escaped declared home")
    for name in list_profile_names():
        resolved = get_profile_dir(name).resolve()
        expected = root if name == "default" else root / "profiles" / name
        if resolved != expected or not resolved.is_relative_to(root):
            raise RuntimeError("lab profile discovery escaped declared home")


if __name__ != "__main__":
    assert_lab_isolation()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import dashboard.plugin_api as plugin_api  # noqa: E402

API_PREFIX = "/api/plugins/hermes-model-switcher"

app = FastAPI()
app.include_router(plugin_api.router, prefix=API_PREFIX)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def _repo_file(rel: str) -> FileResponse:
    return FileResponse(REPO / rel)


@app.get("/fixture-real.html")
@app.get("/")
async def _page():
    # Same-origin fixture so the UI's relative /api/plugins/... base works.
    return _repo_file("tests/fixture-real.html")


@app.get("/tests/vendor.js")
async def _vendor():
    return _repo_file("tests/vendor.js")


@app.get("/ui/dist/plugin.js")
async def _bundle():
    return _repo_file("ui/dist/plugin.js")


@app.get("/dashboard/dist/style.css")
async def _stylesheet():
    return _repo_file("dashboard/dist/style.css")
