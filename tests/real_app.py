"""Real FastAPI app for the Playwright harness: the ACTUAL plugin backend.

Serves dashboard/plugin_api.py's router (the real apply/undo engine over the
real Hermes canonical save seam) against a THROWAWAY lab Hermes home under
/tmp — never the live profile. Providers are mocked only at the credential
level (deterministic fake .env credentials); every Apply/Undo performs real
file saves and real journal writes.

Run with uvicorn:  HERMES_HOME=<lab> python -m uvicorn real_app:app --port 8932
Seed a lab home:   python real_app.py --seed /tmp/some-dir
No inference is ever called: /probe is blocked by design and no provider
network client is constructed.
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
    "default": "model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n",
    "research": "model:\n  provider: openai\n  default: gpt-5.1\n",
    "archived": "model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n",
}
# Deterministic mock credentials (lab-only, never a real provider key; no
# 'sk-' prefix so leak assertions can look for real key shapes safely).
FAKE_CREDENTIAL = "LAB-LOCAL-FAKE-CREDENTIAL-0000000001"


def seed_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(PROFILES["default"])
    for name, raw in PROFILES.items():
        pdir = home / "profiles" / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "config.yaml").write_text(raw)
        (pdir / ".env").write_text(f"OPENAI_API_KEY={FAKE_CREDENTIAL}\n")
    state = home / "plugins" / "hermes-model-switcher"
    state.mkdir(parents=True, exist_ok=True)
    # Seed the reviewed "Fast lane" preset the same way the UI would save it.
    (state / "presets.json").write_text(json.dumps([{
        "preset_id": "p-fast",
        "name": "Fast lane",
        "approved_classes": ["subscription_included", "metered"],
        "allow_unknown": False,
        "reviewed_profile_ids": ["default", "research"],
        "targets": [
            {"profile": "default", "store_kind": "canonical_config",
             "path": str(home / "profiles" / "default" / "config.yaml"),
             "changes": {"model.provider": "zhipu", "model.default": "glm-5.3-flash"},
             "cost_class": "metered"},
            {"profile": "research", "store_kind": "canonical_config",
             "path": str(home / "profiles" / "research" / "config.yaml"),
             "changes": {"model.provider": "zhipu", "model.default": "glm-5.3-flash"},
             "cost_class": "metered"},
        ],
    }]))
    return home


def create_lab_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="msw-e2e-", dir="/tmp"))
    seed_home(home)
    return home


# -- app (module-level; uvicorn imports it with HERMES_HOME in the env) ------
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", help="seed a lab home at this path (or 'auto')")
    ap.add_argument("--home-file", default=str(REPO / "tests" / "real-home.json"))
    args = ap.parse_args()
    if args.seed:
        home = create_lab_home() if args.seed == "auto" else seed_home(Path(args.seed))
        Path(args.home_file).write_text(json.dumps({"home": str(home)}))
        print(home)
