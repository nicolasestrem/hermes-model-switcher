"""T1 skeleton gate: MIT repo, manifest, docs, and clean tracked tree."""
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_]{20,}"),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9\-_]{16,}['\"]", re.I),
]


def git(args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def tracked_files():
    return [l for l in git(["ls-files"]).splitlines() if l]


def test_license_is_mit():
    text = (REPO / "LICENSE").read_text()
    assert "MIT License" in text
    assert "Permission is hereby granted" in text
    assert "Copyright (c)" in text


def test_readme_documents_setup_limitations_recovery():
    text = (REPO / "README.md").read_text()
    low = text.lower()
    assert "## setup" in low, "README missing Setup section"
    assert "## limitations" in low, "README missing Limitations section"
    assert "## recovery" in low, "README missing Recovery section"


def test_manifest_json_valid_and_minimal():
    """The two manifests are different surfaces and neither reads the other's keys.

    ``plugin.yaml`` is the AGENT manifest: the loader imports the plugin
    directory and calls ``register()``. ``dashboard/manifest.json`` is the
    DASHBOARD manifest: the web server reads it and imports the ``api`` file it
    names. Keys the agent loader does not recognise are warned about and
    ignored, so ``entrypoint``/``backend``/``api_prefix`` on ``plugin.yaml``
    bought nothing and the backend path is declared in the dashboard manifest.
    """
    import yaml

    m = yaml.safe_load((REPO / "plugin.yaml").read_text())
    assert m is not None, "plugin.yaml missing"
    for key in ("name", "version", "manifest_version"):
        assert m.get(key), f"plugin.yaml missing {key}"
    assert m["name"] == "hermes-model-switcher"
    assert m.get("kind", "standalone") == "standalone"
    for non_manifest_key in ("entrypoint", "backend", "api_prefix", "permissions"):
        assert non_manifest_key not in m, (
            f"plugin.yaml declares {non_manifest_key!r}, which the agent loader "
            "does not read (it warns and ignores it)")

    dash = json.loads((REPO / "dashboard" / "manifest.json").read_text())
    for key in ("name", "label", "version", "tab", "entry"):
        assert dash.get(key), f"dashboard/manifest.json missing {key}"
    assert dash["name"] == "hermes-model-switcher"
    assert dash["api"] == "plugin_api.py"
    assert dash["tab"]["path"].startswith("/")


def test_plugin_is_loadable_beside_its_manifest():
    """`hermes plugins validate` rejects a plugin.yaml with no __init__.py,
    desktop/plugin.js or plugin.json beside it, and the agent loader records
    "No __init__.py in <dir>" for it at every startup."""
    init = REPO / "__init__.py"
    assert init.is_file(), "root __init__.py is the entry point Hermes imports"
    assert "def register(" in init.read_text(), "must expose register(ctx)"


def test_dashboard_bundle_is_tracked():
    """The manifest's entry is loaded from the INSTALLED directory, and a git
    install never runs npm — so the bundle has to be in the repo, as it is in
    every other dashboard plugin installed alongside this one."""
    bundle = REPO / "dashboard" / "dist" / "index.js"
    assert bundle.is_file(), "run `npm run build`"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "dashboard/dist/index.js"],
        cwd=REPO, capture_output=True, text=True)
    assert tracked.returncode == 0, (
        "dashboard/dist/index.js must be tracked: an ignored bundle makes every "
        "git install serve a 404 for the tab's entry")


def test_package_json_mit_and_private():
    p = json.loads((REPO / "package.json").read_text())
    assert p["license"] == "MIT"
    assert p.get("private") is True
    assert "build" in p.get("scripts", {})


def test_no_secrets_or_host_paths_in_tracked_files():
    # Host paths are forbidden EXCEPT the documented read-only Hermes source
    # checkout used for registry scans — resolved from the environment (env
    # var override, else ~/.hermes/hermes-agent relative to the running
    # user's home), never a baked-in literal in this repo.
    try:
        ref_repo = str(Path(os.environ.get(
            "HERMES_MODEL_SWITCHER_HERMES_REPO",
            Path.home() / ".hermes" / "hermes-agent")).resolve())
    except OSError:
        ref_repo = ""
    offenders = []
    for rel in tracked_files():
        path = REPO / rel
        if not path.is_file() or path.suffix in {".lock", ".png", ".woff", ".ttf"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                offenders.append(rel)
        if ref_repo and ref_repo in text:
            text = text.replace(ref_repo, "")
        if re.search(r"/home/\w", text.replace(str(Path.home()), "~")):
            offenders.append(rel + " (host path)")
    assert offenders == [], f"host-specific data tracked: {offenders}"


def test_node_modules_not_tracked():
    assert not [f for f in tracked_files() if f.startswith("node_modules/")], \
        "node_modules must not be committed"


def test_no_direct_work_on_main():
    """Implementation must land through a PR.

    Being checked out on `main` is normal once a feature is merged, so that
    alone is not a failure. What must never happen is implementation work
    sitting uncommitted on the integration branch, i.e. about to be committed
    straight to main.
    """
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if branch != "main":
        return
    dirty = git(["status", "--porcelain"]).strip()
    assert dirty == "", "uncommitted work on main: commit on a feature branch instead"
