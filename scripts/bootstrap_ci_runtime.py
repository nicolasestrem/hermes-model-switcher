#!/usr/bin/env python3
"""Initialize a genuine Hermes source-identity install stamp in a CI checkout.

Why this exists
---------------
The plugin's test suites import a real Hermes checkout on ``sys.path``
(``tests/conftest.py`` imports ``agent.secret_scope``; ``tests/real_app.py``
imports ``hermes_constants`` / ``hermes_cli``). ``hermes_adapter.probe_runtime``
gates writes on ``hermes_cli.__version__``, and ``hermes_cli/__init__.__getattr__``
reads that from ``install-stamp.json`` -- an installer-generated file that is
git-ignored. A bare CI clone therefore carries no stamp, ``__getattr__`` returns
the pre-stamp placeholder ``0.0.0``, the adapter goes read-only, and the
integration tests fail.

The supported fix is NOT to fake ``__version__``, hand-author a stamp, or weaken
the guard. It is to produce the stamp the way every packager does: run Hermes'
own ``scripts/write_install_stamp.py`` against the pinned checkout, deriving the
base version from the checkout's *verified* Git release tag -- never a literal.

Identity is admitted only when the checkout HEAD, the pinned SHA, and the
commit the annotated tag peels to all agree. The tag alone is not the commit:
an annotated tag object is peeled with ``^{commit}`` before it is trusted.

Usage (CI)::

    python scripts/bootstrap_ci_runtime.py \
        --checkout "$HERMES_MODEL_SWITCHER_HERMES_REPO" \
        --sha "$HERMES_TESTED_SHA" --tag "$HERMES_TESTED_TAG"

Exit 0 = the checkout carries a verified source-identity stamp whose commit and
base version match the pinned tag. Any mismatch is a hard failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

STAMP_NAME = "install-stamp.json"
HELPER_REL = Path("scripts") / "write_install_stamp.py"


def _git(checkout: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(checkout), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _update_channel():
    """The single authority for the tag grammar, imported from the checkout.

    ``hermes_cli.update_channel`` owns ``is_canary_tag`` and ``STABLE_TAG_RE``;
    re-typing those shapes here would let this bootstrap drift from the runtime.
    """
    from hermes_cli import update_channel

    return update_channel


def derive_base_version(tag: str, *, update_channel=None) -> str:
    """The base version a verified release tag names -- never a hand-typed value.

    A canary tag (``vX.Y.Z+canary.<UTC>``) and a stable tag (``vX.Y.Z``) both
    carry their base version as the SemVer core. The core is validated against
    the runtime's own ``STABLE_TAG_RE`` before it is returned.
    """
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError(f"not a release tag: {tag!r}")
    value = tag.strip()
    channel = update_channel or _update_channel()
    if channel.is_canary_tag(value):
        core = value[1:].split("+canary.", 1)[0]
    elif channel.STABLE_TAG_RE.fullmatch(value):
        core = value[1:]
    else:
        raise ValueError(f"unrecognized release tag shape: {tag!r}")
    if not channel.STABLE_TAG_RE.fullmatch("v" + core):
        raise ValueError(f"tag {tag!r} names a non-SemVer base version {core!r}")
    return core


def ensure_tag(checkout: Path, tag: str, remote: str = "origin") -> None:
    """Fetch ``tag`` into the checkout when it is absent (CI clones are bare)."""
    if _git(checkout, "rev-parse", "--verify", f"refs/tags/{tag}"):
        return
    fetched = _git(checkout, "fetch", "--depth", "1", remote, f"refs/tags/{tag}:refs/tags/{tag}")
    if not fetched and not _git(checkout, "rev-parse", "--verify", f"refs/tags/{tag}"):
        raise SystemExit(f"bootstrap: cannot fetch tag {tag!r} from {remote!r} in {checkout}")


def verify_identity(checkout: Path, sha: str, tag: str) -> str:
    """Admit identity only when HEAD, the pinned SHA, and the peeled tag agree.

    Returns the peeled tag commit. An annotated tag is peeled with ``^{commit}``;
    the tag object's own id is never accepted as a commit.
    """
    if not checkout.is_dir():
        raise SystemExit(f"bootstrap: checkout {checkout} does not exist")
    head = _git(checkout, "rev-parse", "HEAD")
    if head is None:
        raise SystemExit(f"bootstrap: cannot read HEAD of {checkout}")
    if head != sha:
        raise SystemExit(f"bootstrap: checkout HEAD {head[:12]} != pinned SHA {sha[:12]}")
    ensure_tag(checkout, tag)
    peeled = _git(checkout, "rev-parse", f"{tag}^{{commit}}")
    if peeled is None:
        raise SystemExit(f"bootstrap: tag {tag!r} does not peel to a commit")
    if peeled != sha:
        raise SystemExit(
            f"bootstrap: tag {tag!r} peels to {peeled[:12]}, not the pinned SHA {sha[:12]}"
        )
    if peeled != head:
        raise SystemExit(f"bootstrap: peeled tag {peeled[:12]} != HEAD {head[:12]}")
    return peeled


def write_stamp(checkout: Path, sha: str, base_version: str, *,
                update_mechanism: str = "self", source: str = "git",
                python: str | None = None) -> Path:
    """Run Hermes' own stamp writer against the checkout; return the stamp path."""
    helper = checkout / HELPER_REL
    if not helper.is_file():
        raise SystemExit(f"bootstrap: {helper} is missing -- checkout is not a Hermes source tree")
    out = checkout / STAMP_NAME
    cmd = [
        python or sys.executable, str(helper),
        "--output", str(out),
        "--commit", sha,
        "--base-version", base_version,
        "--source", source,
        "--update-mechanism", update_mechanism,
    ]
    result = subprocess.run(cmd, cwd=str(checkout), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"bootstrap: write_install_stamp.py failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return out


def read_stamp(stamp_path: Path) -> dict:
    data = json.loads(stamp_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SystemExit(f"bootstrap: {stamp_path} is not a JSON object")
    return data


def verify_stamp(stamp: dict, sha: str, base_version: str) -> None:
    """Read back the written stamp and prove it tells the truth about the checkout."""
    if stamp.get("commit") != sha:
        raise SystemExit(f"bootstrap: stamp commit {stamp.get('commit')!r} != pinned SHA {sha!r}")
    if str(stamp.get("baseVersion") or "") != base_version:
        raise SystemExit(
            f"bootstrap: stamp baseVersion {stamp.get('baseVersion')!r} != tag base {base_version!r}"
        )
    if not stamp.get("updateMechanism"):
        raise SystemExit("bootstrap: stamp carries no updateMechanism")


def initialize(checkout: Path, sha: str, tag: str, *, update_mechanism: str = "self",
               source: str = "git", python: str | None = None) -> dict:
    """Full admission path: verify tag<->SHA<->HEAD, write, read back, verify."""
    verify_identity(checkout, sha, tag)
    # Resolve the tag grammar from THIS checkout, not from any ambient install.
    if str(checkout) not in sys.path:
        sys.path.insert(0, str(checkout))
    base_version = derive_base_version(tag)
    stamp_path = write_stamp(checkout, sha, base_version, update_mechanism=update_mechanism,
                             source=source, python=python)
    stamp = read_stamp(stamp_path)
    verify_stamp(stamp, sha, base_version)
    return stamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path,
                        help="The pinned Hermes checkout (HERMES_MODEL_SWITCHER_HERMES_REPO)")
    parser.add_argument("--sha", required=True, help="The pinned tested commit (HERMES_TESTED_SHA)")
    parser.add_argument("--tag", required=True, help="The verified release tag (HERMES_TESTED_TAG)")
    parser.add_argument("--update-mechanism", default="self",
                        help="Who applies the next update (default: self, as a source checkout)")
    parser.add_argument("--source", default="git", help="Stamp provenance label (default: git)")
    args = parser.parse_args(argv)

    stamp = initialize(args.checkout.resolve(), args.sha, args.tag,
                       update_mechanism=args.update_mechanism, source=args.source)
    print(f"[bootstrap_ci_runtime] {args.checkout}/{STAMP_NAME} "
          f"commit={stamp['commit'][:12]} baseVersion={stamp['baseVersion']} "
          f"source={stamp['source']} tag={args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
