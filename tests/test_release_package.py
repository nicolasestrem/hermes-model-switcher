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


@pytest.mark.parametrize("source", ["docs", "src/model_switcher"])
def test_output_inside_recursive_input_is_rejected(source, monkeypatch):
    builder = load_builder()
    parent = builder.REPO / source
    destination = parent / builder.NAME
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(parent)])
    with pytest.raises(SystemExit):
        builder.main()
    assert not destination.exists()


def test_parent_symlink_input_preserves_output(tmp_path, monkeypatch):
    builder = load_builder()
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "required.txt").write_text("private")
    try:
        (repo / "linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    (repo / "docs").mkdir()
    (repo / "src" / builder.PY_PACKAGE).mkdir(parents=True)
    monkeypatch.setattr(builder, "REPO", repo)
    monkeypatch.setattr(builder, "DOC_FILES", [])
    monkeypatch.setattr(builder, "COPY_FILES", ["linked/required.txt"])
    monkeypatch.setattr(builder, "PY_MODULES", [])
    monkeypatch.setattr(builder, "DASHBOARD", [])
    for path in ["scripts/privacy_guard.py", "dashboard/dist/index.js", "dashboard/dist/style.css"]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    output = tmp_path / "output" / builder.NAME
    output.mkdir(parents=True)
    (output / "keep.txt").write_text("preserve")
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(output.parent), "--replace"])
    with pytest.raises(SystemExit):
        builder.main()
    assert (output / "keep.txt").read_text() == "preserve"


@pytest.mark.parametrize("unsafe", [False, True])
def test_output_parent_alias_uses_canonical_containment(tmp_path, monkeypatch, unsafe):
    builder = load_builder()
    target = builder.REPO / "docs" if unsafe else tmp_path / "safe"
    target.mkdir(exist_ok=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(alias)])
    if unsafe:
        with pytest.raises(SystemExit):
            builder.main()
        assert not (target / builder.NAME).exists()
    else:
        builder.main()
        assert (target / builder.NAME / "README.md").is_file()


def test_unapproved_docs_are_never_packaged(tmp_path, monkeypatch):
    import shutil
    builder = load_builder()
    repo = tmp_path / "checkout"
    shutil.copytree(builder.REPO / "docs", repo / "docs")
    for relative in builder.COPY_FILES + builder.PY_MODULES + ["scripts/privacy_guard.py"]:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(builder.REPO / relative, destination)
    shutil.copytree(builder.REPO / "dashboard", repo / "dashboard")
    shutil.copytree(builder.REPO / "src", repo / "src")
    for relative in ["local-notes.md", "exports/config.yaml", "images/raw-private.png"]:
        path = repo / "docs" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PRIVATE-SYNTHETIC-SENTINEL")
    monkeypatch.setattr(builder, "REPO", repo)
    monkeypatch.setattr("sys.argv", ["build_package.py", "--out", str(tmp_path / "output")])
    builder.main()
    docs = tmp_path / "output" / builder.NAME / "docs"
    assert sorted(str(p.relative_to(docs)).replace(chr(92), "/") for p in docs.rglob("*") if p.is_file()) == sorted(builder.DOC_FILES)
    assert not any(b"PRIVATE-SYNTHETIC-SENTINEL" in p.read_bytes() for p in docs.rglob("*") if p.is_file())
