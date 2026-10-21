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

# ``model_catalog.enabled: false`` is Hermes' own offline switch for the remote
# model-catalog manifest (hermes_cli.model_catalog.get_catalog: disabled ->
# returns {} and fetches nothing). The lab never wants that fetch.
_OFFLINE_BLOCK = "model_catalog:\n  enabled: false\n"

PROFILES = {
    "default": (
        "model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n"
        "providers:\n"
        "  zhipu:\n    base_url: http://127.0.0.1:8080/v1\n"
        + _OFFLINE_BLOCK
    ),
    "research": (
        "model:\n  provider: openai\n  default: gpt-5.1\n"
        "providers:\n"
        "  zhipu:\n    base_url: http://127.0.0.1:8080/v1\n"
        + _OFFLINE_BLOCK
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


def lab_parent_dir() -> Path:
    """The directory a lab home is minted under: ``TMPDIR`` when it is usable, else ``/tmp``.

    ``TMPDIR`` is honoured only when the Hermes resolver treats a path beneath it
    as its own root. A ``TMPDIR`` nested under the native ``~/.hermes`` resolves
    back to the live home (``_hermes_root_of``: anything under ``~/.hermes`` *is*
    ``~/.hermes``), so a lab there would mean profile writes against the real
    tree — the exact thing this harness must never do. Nothing is created to
    decide: the resolver is asked about a hypothetical child path.
    """
    from hermes_constants import get_default_hermes_root

    native = get_default_hermes_root(home="").resolve(strict=False)
    seen: list[Path] = []
    for raw in (os.environ.get("TMPDIR"), "/tmp"):
        if not raw:
            continue
        parent = Path(raw).expanduser().resolve(strict=False)
        if parent in seen:
            continue
        seen.append(parent)
        probe = parent / "msw-e2e-isolation-probe"
        if parent == native:
            continue
        if get_default_hermes_root(home=str(probe)).resolve(strict=False) == probe:
            return parent
    return Path("/tmp")


def create_lab_home() -> Path:
    parent = lab_parent_dir()
    declared = os.environ.get("TMPDIR")
    if declared and Path(declared).expanduser().resolve(strict=False) != parent:
        print(f"lab home: TMPDIR={declared} is not an isolated Hermes root, "
              f"minting under {parent}", file=sys.stderr)
    home = assert_seed_target_isolated(
        Path(tempfile.mkdtemp(prefix="msw-e2e-", dir=str(parent))))
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
    ap.add_argument("--print-lab-parent", action="store_true",
                    help="print the directory a lab home may be minted under and exit")
    args = ap.parse_args()
    if args.print_profiles:
        print(json.dumps(PROFILES))
        raise SystemExit(0)
    if args.print_lab_parent:
        print(lab_parent_dir())
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


# -- offline lab ------------------------------------------------------------
# The lab must never touch the network. Three layers, in the order Hermes itself
# uses them:
#   1. config switches (``model_catalog.enabled: false`` in the seeded config),
#   2. warm on-disk caches — models.dev, the per-provider model-id cache and the
#      reasoning-caps mirror — so every read path serves cache and no
#      stale-while-revalidate / background warmer fires,
#   3. a socket-level guard that refuses any remaining non-loopback connect, so
#      a missed path fails loudly (and is recorded) instead of leaking out.
# Layer 3 is the guarantee; 1+2 are what keep it from being exercised.
NETWORK_ATTEMPTS: list[dict] = []
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "0.0.0.0", "")


def block_outbound_network() -> None:
    """Refuse every connection that leaves this machine; record the attempt.

    Loopback stays allowed (the harness talks to the lab over 127.0.0.1) and
    non-IP families (AF_UNIX, used by subprocess plumbing) pass through. A
    refused attempt raises inside the caller exactly like an unreachable
    network would, so Hermes' own fail-soft paths behave normally.
    """
    import socket as _socket
    import threading as _threading

    if getattr(_socket.socket.connect, "_hms_lab_offline_guard", False):
        return
    real_connect = _socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else None
        if not isinstance(host, str):
            return real_connect(self, address, *args, **kwargs)
        if host in _LOOPBACK_HOSTS or host.startswith("127."):
            return real_connect(self, address, *args, **kwargs)
        NETWORK_ATTEMPTS.append({
            "host": host,
            "port": address[1] if len(address) > 1 else None,
            "thread": _threading.current_thread().name,
        })
        raise OSError(f"lab is offline: outbound connection to {host} refused")

    guarded_connect._hms_lab_offline_guard = True  # type: ignore[attr-defined]
    _socket.socket.connect = guarded_connect


def _profile_homes(home: Path) -> list[Path]:
    """The launch home plus every named profile home that holds a config."""
    homes = [home]
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if child.is_dir() and (child / "config.yaml").exists():
                homes.append(child)
    return homes


def install_offline_caches() -> dict:
    """Warm the caches Hermes' model read paths consult (no network involved).

    Every value is derived from local data — the in-repo curated provider tables
    (``hermes_cli.models_catalog_static._PROVIDER_MODELS``) and empty registries
    that say "unknown" rather than inventing per-model facts. Rows are written
    with the credential fingerprint the SERVING process resolves, which is why
    this runs at app import and not in the seed CLI.

    Caches are per HOME, and the routers run each request inside the requested
    profile's home (``_profile_scope``), so every profile home gets its own set —
    a cache only in the launch home leaves a ``?profile=`` read cold and it would
    probe the network.
    """
    import time as _time

    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.models import _credential_fingerprint
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS

    launch_home = Path(get_hermes_home())
    written: dict[str, object] = {}
    written["homes:"] = [str(p) for p in _profile_homes(launch_home)]

    # 1) models.dev registry: present and fresh. Providers carry no models, so
    #    capability lookups answer "unknown" instead of fabricating metadata.
    models_dev = {
        slug: {"id": slug, "name": slug, "models": {}}
        for slug in ("anthropic", "openai", "openrouter", "nous")
    }

    # 2) reasoning-capability mirror: a warm entry per catalog URL, so
    #    _warm_caps_async never spawns its fetch thread. The single placeholder
    #    id cannot collide with a real model, so nothing real is asserted about.
    caps_ts = _time.time()
    caps = {"lab-offline-placeholder": {"supports_reasoning": False}}
    mirror: dict[str, object] = {
        "https://openrouter.ai/api/v1/models": {"ts": caps_ts, "caps": caps},
    }
    try:
        from hermes_cli.models_reasoning_caps import nous_catalog_url
        mirror[nous_catalog_url()] = {"ts": caps_ts, "caps": caps}
    except Exception:
        # Unresolvable Portal URL: the guard still catches that fetch.
        pass

    for home in _profile_homes(launch_home):
        token = set_hermes_home_override(str(home))
        try:
            home.mkdir(parents=True, exist_ok=True)
            # Hermes materialises $HERMES_HOME/auth.json on the first credential
            # read (env-seeded pool rows are persisted), and ``auth.json``'s
            # existence/mtime is part of the provider fingerprint — so the first
            # real request would invalidate rows fingerprinted before it. Read a
            # pool first, exactly as the picker does, then fingerprint.
            for slug in ("openai-api", "openai", "anthropic", "openrouter"):
                try:
                    from agent.credential_pool import load_pool
                    load_pool(slug)
                except Exception:
                    pass
            (home / "models_dev_cache.json").write_text(json.dumps(models_dev))
            # 3) per-provider model-id cache: rows come from the curated static
            #    tables (Hermes' own offline catalog), fingerprinted for THIS
            #    home's credentials, so cached_provider_model_ids serves "fresh"
            #    and probes nothing.
            rows: dict[str, object] = {}
            now = _time.time()
            for slug, models in _PROVIDER_MODELS.items():
                if not models:
                    continue
                try:
                    rows[slug] = {"fp": _credential_fingerprint(slug), "at": now,
                                  "models": list(models)}
                except Exception:
                    continue
            (home / "provider_models_cache.json").write_text(json.dumps(rows))
            cache_dir = home / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "reasoning_caps.json").write_text(json.dumps(mirror))
            # 4) OpenRouter model-metadata mirror (pricing/context lookups):
            #    fresh placeholder row, so an expensive-model guard asking for
            #    pricing reads the cache instead of fetching openrouter.ai.
            (cache_dir / "openrouter_model_metadata.json").write_text(json.dumps({
                "lab-offline-placeholder": {"context_length": 0, "pricing": {}},
            }))
        finally:
            reset_hermes_home_override(token)

    written["models_dev"] = sorted(models_dev)
    written["reasoning_caps"] = sorted(mirror)
    written["provider_models"] = "curated static tables per provider"
    return written


_OFFLINE_CACHES: dict = {}
block_outbound_network()
_OFFLINE_CACHES = install_offline_caches()


# -- real host model routes -------------------------------------------------
# The dashboard's /api/model/* surface is Hermes' own extracted router
# (hermes_cli/web_routers/models.py): route bodies, request validation, catalog
# assembly and the config writes are Hermes' code, mounted unmodified. A host
# has to supply the process state those routers resolve late from
# ``hermes_cli.web_server`` (hermes_cli.web_deps). For these routes that is one
# symbol — the config-mutation lock — so the lab supplies exactly that instead
# of assembling the whole dashboard app (whose upload routers need
# python-multipart, absent here). If a future Hermes route starts resolving more
# web_server state, calls fail with AttributeError and the host-route tests fail:
# nothing is silently stubbed.
HOST_STATE_SYMBOLS = ("_CONFIG_MUTATION_LOCK",)


def install_host_state():
    """Install the minimal ``hermes_cli.web_server`` state the routers late-bind."""
    import threading
    import types

    existing = sys.modules.get("hermes_cli.web_server")
    if existing is not None:
        return existing
    module = types.ModuleType("hermes_cli.web_server")
    module.__doc__ = "Lab-host state for hermes_cli.web_routers.* (tests/real_app.py)."
    setattr(module, "_CONFIG_MUTATION_LOCK", threading.RLock())
    sys.modules["hermes_cli.web_server"] = module
    import hermes_cli
    setattr(hermes_cli, "web_server", module)
    return module


from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import dashboard.plugin_api as plugin_api  # noqa: E402
from hermes_cli.web_routers import models as host_model_routes  # noqa: E402

API_PREFIX = "/api/plugins/hermes-model-switcher"

app = FastAPI()
app.include_router(plugin_api.router, prefix=API_PREFIX)
install_host_state()
app.include_router(host_model_routes.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/lab/offline")
async def lab_offline():
    """What the lab is holding the network off with — for tests and evidence."""
    return {
        # Structural, not observational: every non-loopback connect raises in
        # block_outbound_network, so nothing can reach the network from here.
        "offline": True,
        "outbound_connections": 0,
        "guard": "socket.connect refuses every non-loopback address",
        "refused_network_attempts": list(NETWORK_ATTEMPTS),
        "config_switches": {"model_catalog.enabled": False},
        "host_routes": [
            "GET /api/model/options",
            "GET /api/model/auxiliary",
            "GET /api/model/moa",
            "POST /api/model/set",
        ],
        "caches": _OFFLINE_CACHES,
    }


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
