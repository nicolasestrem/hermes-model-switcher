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
    import yaml
    m = yaml.safe_load((REPO / "plugin.yaml").read_text())
    # plugin.yaml is the dashboard manifest; validate required keys
    assert m is not None, "plugin.yaml missing"
    for key in ("name", "version", "entrypoint", "backend", "api_prefix", "permissions"):
        assert m.get(key), f"plugin.yaml missing {key}"
    assert m["name"] == "hermes-model-switcher"
    assert m["api_prefix"] == "/api/plugins/hermes-model-switcher"


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


def test_feature_branch_active():
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    assert branch != "main", "implementation must happen off main"
