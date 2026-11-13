"""Scan tracked and untracked, non-ignored files for personal data and secrets.

Prints every match as `file:line  kind  masked value`, sorted. Values are masked (first two
characters and the length) so the output can be pasted into a report. Matches are
candidates: the reviewer opens each one and decides whether it's real.

- icp: ICP-shaped strings other than the fake ones in ALLOWED_ICPS.
- email: addresses outside ALLOWED_EMAIL_DOMAINS.
- phone: NZ phone-shaped numbers.
- jwt, bearer, cookie, private-key: token- and secret-shaped values.
- json-key: non-empty string or number values under keys whose name contains a personal part
  (PERSONAL_KEYS) in JSON files.

Candidates already confirmed fake are listed in reviewed.txt by a hash of their file, kind,
key path and value (never the value itself), and aren't printed again, so a clean tree prints
"none". Once every printed candidate has been checked and is fake, `--record` adds them to
reviewed.txt. A hash stops matching when the file, key or value changes, and is then listed
under STALE REVIEWED for removal.

With `--message FILE`, it checks a commit message instead, for ICPs and secrets, and exits
with an error if any match (the `commit-no-personal-data` hook). Email addresses and phone
numbers are left out there, as Dependabot's messages quote release notes that have them.
Nothing in a commit message is ever reviewed as fake, since it can't be removed once pushed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from _repo import SKILL_DIR, read_text, repo_files

REVIEWED = SKILL_DIR / "scripts/reviewed.txt"
# Kinds a commit message may have: release notes Dependabot quotes have addresses and numbers.
MESSAGE_SKIPPED = {"email", "phone"}
# The fake ICPs the tests use. Anything else ICP-shaped is reported.
ALLOWED_ICPS = {"0000123456AB1C2", "0000654321AB1C2", "0000999999AB1C2", "9999999999ZZ9Z9"}
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.nz",
    "users.noreply.github.com",
    "noreply.github.com",
)
# Lock files are full of hashes, and glyphs.py only holds hex glyph templates.
SKIPPED_FILES = {
    "uv.lock",
    "frontend/package-lock.json",
    "custom_components/ecotricity_nz/api/glyphs.py",
}
# UI text, where personal keys such as "password" are field labels.
LABEL_FILES = {
    "custom_components/ecotricity_nz/strings.json",
    "custom_components/ecotricity_nz/translations/en.json",
}
PATTERNS = {
    "icp": re.compile(r"(?<![0-9A-Za-z])\d{10}[A-Za-z]{2}[0-9A-Za-z]{3}(?![0-9A-Za-z])"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "phone": re.compile(
        r"(?<![\d.])(?:\+64[\s-]?|0)(?:2\d|[3-9])[\s-]?\d{3}[\s-]?\d{3,4}(?![\d.])"
    ),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    "cookie": re.compile(r"(?i)(?:JSESSIONID|PHPSESSID|session(?:id)?)=[A-Za-z0-9._%-]{12,}"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
# Parts of key names that hold personal data or secrets, matched anywhere in a key once it's
# lower-cased with everything but letters and digits dropped (so meterSerialNumber, icp_address
# and vGPS_Easting all match). New fields from Ecotricity are caught without adding them here.
# A plan's customer_type ("RESIDENTIAL") is the market it's sold to, not a person.
PERSONAL_KEYS = re.compile(
    r"address|street|suburb|town|postcode|propertyname|unitnumber|firstname|lastname|fullname|"
    r"email|phone|mobile|password|token|cookie|session|account|contract|customer(?!type)|serial|"
    r"gps|easting|northing|userid|userref"
)


def _mask(value: str) -> str:
    return f"{value[:2]}... ({len(value)} chars)"


def _digest(path: Path, kind: str, value: str) -> str:
    return hashlib.sha256(f"{path.as_posix()}\0{kind}\0{value}".encode()).hexdigest()


def _text_matches(path: Path, text: str) -> list[tuple[str, str]]:
    """Return (hash, line) for each match."""
    found: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS.items():
            for match in pattern.findall(line):
                if kind == "icp" and match.upper() in ALLOWED_ICPS:
                    continue
                if kind == "email" and match.lower().endswith(ALLOWED_EMAIL_DOMAINS):
                    continue
                where = f"{path.as_posix()}:{number}  {kind}  {_mask(match)}"
                found.append((_digest(path, kind, match), where))
    return found


def _json_matches(path: Path, text: str) -> list[tuple[str, str]]:
    """Return (hash, line) for each personal key's value."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    found: list[tuple[str, str]] = []

    def walk(value: Any, where: str) -> None:  # noqa: ANN401 - walks arbitrary JSON
        if isinstance(value, dict):
            for key, child in value.items():
                child_where = f"{where}.{key}"
                if (
                    PERSONAL_KEYS.search(re.sub(r"[^a-z0-9]", "", str(key).lower()))
                    and isinstance(child, str | int)
                    and str(child)
                ):
                    line = f"{path.as_posix()}  json-key  {child_where} = {_mask(str(child))}"
                    found.append((_digest(path, child_where, str(child)), line))
                walk(child, child_where)
        elif isinstance(value, list):
            for child in value:
                walk(child, f"{where}[]")

    walk(payload, "$")
    return sorted(set(found))


def _reviewed() -> dict[str, str]:
    """Return each reviewed hash with the note beside it."""
    if not REVIEWED.exists():
        return {}
    entries: dict[str, str] = {}
    for line in REVIEWED.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, _, note = line.partition("  ")
            entries[digest] = note
    return entries


def _check_message(path: Path) -> None:
    found = [
        line
        for _, line in _text_matches(path, path.read_text(encoding="utf-8"))
        if line.split("  ")[1] not in MESSAGE_SKIPPED
    ]
    if found:
        print("\n".join(found))
        sys.exit(1)


def main() -> None:
    """Print every candidate not yet reviewed, or record them all with --record."""
    if sys.argv[1:2] == ["--message"]:
        _check_message(Path(sys.argv[2]))
        return
    results: list[tuple[str, str]] = []
    for path in repo_files():
        if path.as_posix() in SKIPPED_FILES:
            continue
        text = read_text(path)
        if text is None:
            continue
        results.extend(_text_matches(path, text))
        if path.suffix == ".json" and path.as_posix() not in LABEL_FILES:
            results.extend(_json_matches(path, text))
    reviewed = _reviewed()
    if "--record" in sys.argv[1:]:
        for digest, line in results:
            reviewed.setdefault(digest, line.split("  = ")[0].rsplit("  ", 1)[0])
        header = "# Candidates from pii_scan.py confirmed fake by hand: sha256, then file only.\n"
        body = "".join(f"{digest}  {note}\n" for digest, note in sorted(reviewed.items()))
        REVIEWED.write_text(header + body, encoding="utf-8")
        print(f"recorded {len(results)} candidates in {REVIEWED.name}")
        return
    new = sorted({line for digest, line in results if digest not in reviewed})
    stale = sorted(set(reviewed) - {digest for digest, _ in results})
    print(f"# candidates: {len(results)}, already reviewed: {len(results) - len(new)}")
    print(f"\n## CANDIDATES ({len(new)})")
    print("\n".join(new) or "none")
    print(f"\n## STALE REVIEWED ({len(stale)})")
    print("\n".join(f"{digest}  {reviewed[digest]}" for digest in stale) or "none")


if __name__ == "__main__":
    main()
