"""List the review's recorded decisions, and the files changed since the last review.

decisions.txt beside this script holds review candidates already decided, one per line:
`CHECK-ID | path | anchor | status | reason`. The status is `dismissed` (checked, not an
issue), `wontfix` (a real issue, accepted as it is) or `open` (a finding waiting on the
maintainer). The anchor is text that must still be in the file, usually a function's name or a
short line of it. Once the file or the anchor is gone, the code has changed, so the decision no
longer holds.

.state/reviewed.txt, git-ignored, holds each file's hash as a review left it. `--record`
writes it at the end of a review, and the next review applies its judgement checks only to
the files changed since.

- DECISIONS: dismissed and wontfix decisions that still hold. The review never raises these
  again.
- OPEN: findings waiting on the maintainer. The review reports them again as they are.
- STALE DECISIONS: decisions whose file or anchor is gone. The review removes them.
- INVALID DECISIONS: lines not in the format above, or naming a check SKILL.md doesn't have.
- CHANGED SINCE LAST REVIEW: files added, changed or removed since `--record` last ran, or
  "all" if it never has.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from _repo import ROOT, SKILL_DIR, read_text, repo_files
from review_tags import CHECK

DECISIONS = SKILL_DIR / "scripts/decisions.txt"
STATE = SKILL_DIR / ".state/reviewed.txt"
STATUSES = {"dismissed", "wontfix", "open"}
FIELDS = 5


def _hashes() -> dict[str, str]:
    return {
        path.as_posix(): hashlib.sha256((ROOT / path).read_bytes()).hexdigest()[:16]
        for path in repo_files()
    }


def _decisions(checks: set[str]) -> dict[str, list[str]]:
    """Return the decisions that hold, the open ones, the stale ones and the invalid lines."""
    found: dict[str, list[str]] = {
        "DECISIONS": [],
        "OPEN": [],
        "STALE DECISIONS": [],
        "INVALID DECISIONS": [],
    }
    for number, line in enumerate(DECISIONS.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if (
            len(parts) != FIELDS
            or parts[0] not in checks
            or parts[3] not in STATUSES
            or not all(parts)
        ):
            found["INVALID DECISIONS"].append(f"line {number}: {line}")
            continue
        check, path, anchor, status, reason = parts
        text = read_text(Path(path)) if (ROOT / path).is_file() else None
        if text is None or anchor not in text:
            section = "STALE DECISIONS"
        else:
            section = "OPEN" if status == "open" else "DECISIONS"
        found[section].append(f"{check} {path}: {anchor} ({status}: {reason})")
    return found


def main() -> None:
    """Print the decisions and changed files, or record the tree's hashes with --record."""
    current = _hashes()
    if "--record" in sys.argv[1:]:
        STATE.parent.mkdir(exist_ok=True)
        lines = (f"{digest}  {path}" for path, digest in sorted(current.items()))
        STATE.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        print(f"recorded {len(current)} files in {STATE.name}")
        return

    checks = set(CHECK.findall((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")))
    for heading, entries in _decisions(checks).items():
        print(f"## {heading} ({len(entries)})")
        print("\n".join(entries) or "none", end="\n\n")

    if not STATE.is_file():
        print("## CHANGED SINCE LAST REVIEW (all)\nno review recorded")
        return
    recorded = {
        path: digest
        for digest, path in (
            line.split("  ", 1) for line in STATE.read_text(encoding="utf-8").splitlines()
        )
    }
    changed = sorted(
        path for path in current.keys() | recorded.keys() if current.get(path) != recorded.get(path)
    )
    print(f"## CHANGED SINCE LAST REVIEW ({len(changed)})")
    print("\n".join(changed) or "none")


if __name__ == "__main__":
    main()
