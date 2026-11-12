"""Scan tracked and untracked, non-ignored files for personal data and secrets.

Prints every match as `file:line  kind  masked value`, sorted. Values are masked (first two
characters and the length) so the output can be pasted into a report. Matches are
candidates: the reviewer opens each one and decides whether it's real.

- icp: ICP-shaped strings other than the fake ones in ALLOWED_ICPS.
- email: addresses outside ALLOWED_EMAIL_DOMAINS.
- phone: NZ phone-shaped numbers.
- jwt, bearer, cookie, private-key: token- and secret-shaped values.
- json-key: non-empty string values under personal keys (PERSONAL_KEYS) in JSON files.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from _repo import read_text, repo_files

if TYPE_CHECKING:
    from pathlib import Path

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
PERSONAL_KEYS = re.compile(
    r"(?i)^(address|street|suburb|town|postcode|propertyname|unitnumber|firstname|lastname|"
    r"fullname|email|phone|mobile|password|token|cookie|session|accountnumber|account|"
    r"contractnumber|customernumber|customerid|serialnumber|vmeteringcomponentserialnumber|"
    r"vgps_e|vgps_n|vgps_easting|vgps_northing|userid|userref)$"
)


def _mask(value: str) -> str:
    return f"{value[:2]}... ({len(value)} chars)"


def _text_matches(path: Path, text: str) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS.items():
            for match in pattern.findall(line):
                if kind == "icp" and match.upper() in ALLOWED_ICPS:
                    continue
                if kind == "email" and match.lower().endswith(ALLOWED_EMAIL_DOMAINS):
                    continue
                found.append(f"{path.as_posix()}:{number}  {kind}  {_mask(match)}")
    return found


def _json_matches(path: Path, text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    found: list[str] = []

    def walk(value: Any, where: str) -> None:  # noqa: ANN401 - walks arbitrary JSON
        if isinstance(value, dict):
            for key, child in value.items():
                child_where = f"{where}.{key}"
                if PERSONAL_KEYS.match(str(key)) and isinstance(child, str | int) and str(child):
                    found.append(
                        f"{path.as_posix()}  json-key  {child_where} = {_mask(str(child))}"
                    )
                walk(child, child_where)
        elif isinstance(value, list):
            for child in value:
                walk(child, f"{where}[]")

    walk(payload, "$")
    return sorted(set(found))


def main() -> None:
    """Print every candidate."""
    results: list[str] = []
    for path in repo_files():
        if path.as_posix() in SKIPPED_FILES:
            continue
        text = read_text(path)
        if text is None:
            continue
        results.extend(_text_matches(path, text))
        if path.suffix == ".json" and path.as_posix() not in LABEL_FILES:
            results.extend(_json_matches(path, text))
    print(f"## CANDIDATES ({len(results)})")
    print("\n".join(sorted(results)) or "none")


if __name__ == "__main__":
    main()
