"""Shared helpers for the full-review inventory scripts."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = Path(__file__).resolve().parents[1]


def repo_files() -> list[Path]:
    """Return tracked and untracked, non-ignored files, sorted, relative to the repo root."""
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607 - git on PATH
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    paths = {Path(name) for name in output.split("\0") if name}
    return sorted(path for path in paths if (ROOT / path).is_file())


def read_text(path: Path) -> str | None:
    """Return a file's text, or None if it isn't UTF-8 text."""
    try:
        return (ROOT / path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
