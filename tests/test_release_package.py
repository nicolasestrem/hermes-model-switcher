"""Release assembly must preserve sources and ship navigable documentation."""
import importlib.util
import re
from pathlib import Path

import pytest


def load_builder():
    spec = importlib.util.spec_from_file_location("release_builder", Path(__file__).resolve().parents[1] / "scripts/build_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_contains_readme_link_targets(tmp_path, monkeypatch):
    builder = load_builder()
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(tmp_path)])
    builder.main()
    package = tmp_path / builder.NAME
    for target in re.findall(r"\]\(([^)]+)\)", (package / "README.md").read_text()):
        if "://" not in target:
            assert (package / target).is_file(), target


def test_existing_output_requires_explicit_replacement(tmp_path, monkeypatch):
    builder = load_builder()
    out = tmp_path / builder.NAME
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("preserve")
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        builder.main()
    assert sentinel.read_text() == "preserve"


def test_missing_input_preserves_previous_output(tmp_path, monkeypatch):
    builder = load_builder()
    out = tmp_path / builder.NAME
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("preserve")
    monkeypatch.setattr(builder, "COPY_FILES", ["missing-required-file"])
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(tmp_path), "--replace"])
    with pytest.raises(SystemExit):
        builder.main()
    assert sentinel.read_text() == "preserve"


def test_cannot_replace_source_checkout(monkeypatch):
    builder = load_builder()
    # Remote candidate checkouts need not be named like the distributed package.
    monkeypatch.setattr(builder, "NAME", builder.REPO.name)
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(builder.REPO.parent), "--replace"])
    with pytest.raises(SystemExit):
        builder.main()
    assert (builder.REPO / "README.md").is_file()
