"""Check the docs' code references and the integration's UI metadata against the code.

Sections, all sorted:

- MISSING PATHS: backticked paths in the docs that don't exist.
- MISSING NAMES: backticked identifiers in the docs that appear nowhere in the code, tests,
  scripts, frontend or config. Some are external names (Home Assistant, libraries); the
  reviewer classifies each one.
- README ENTITIES: entity names in README.md's entity tables that strings.json doesn't have,
  and strings.json entity names the README doesn't list.
- TRANSLATION KEYS: strings.json keys with no icon, keys the code never uses, and keys the
  code uses that strings.json doesn't have; translations/en.json against strings.json.
- SERVICES: services.yaml, strings.json, icons.json and services.py against each other.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from _repo import ROOT, read_text, repo_files

if TYPE_CHECKING:
    from pathlib import Path

INTEGRATION = "custom_components/ecotricity_nz"
DOCS = ("README.md", "CLAUDE.md", "CHANGELOG.md")
CODE_ROOTS = (
    "custom_components/",
    "tests/",
    "scripts/",
    "frontend/src/",
    "frontend/test/",
    ".github/",
)
CODE_FILES = {
    "pyproject.toml",
    "hacs.json",
    ".pre-commit-config.yaml",
    ".gitignore",
    "frontend/package.json",
    "frontend/tsconfig.json",
}
PATH_PREFIXES = (
    "",
    f"{INTEGRATION}/",
    f"{INTEGRATION}/api/",
    "frontend/",
    "frontend/src/",
    "tests/",
    "docs/",
    "scripts/",
    ".claude/skills/full-review/scripts/",
)
PATH_SUFFIXES = (".py", ".ts", ".js", ".json", ".md", ".yaml", ".yml", ".toml", ".png", ".lock")
FENCE = re.compile(r"^\s*(```|~~~)")
SPAN = re.compile(r"`([^`\n]+)`")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(\(\))?$")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _doc_files(files: list[Path]) -> list[Path]:
    return [
        path
        for path in files
        if path.as_posix() in DOCS or (path.as_posix().startswith("docs/") and path.suffix == ".md")
    ]


def _doc_lines(path: Path) -> list[tuple[int, str]]:
    """Return a doc's lines outside code fences. CHANGELOG.md only up to its first release."""
    text = read_text(path) or ""
    lines: list[tuple[int, str]] = []
    fenced = False
    headings = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if path.as_posix() == "CHANGELOG.md" and line.startswith("## "):
            headings += 1
            if headings > 1:
                break
        if not fenced:
            lines.append((number, line))
    return lines


def _path_exists(span: str) -> bool:
    candidate = span.rstrip("/")
    return any((ROOT / f"{prefix}{candidate}").exists() for prefix in PATH_PREFIXES)


def _looks_like_path(span: str) -> bool:
    if " " in span or any(mark in span for mark in "{}<>*$:="):
        return False
    return "/" in span or span.endswith(PATH_SUFFIXES)


def _check_spans(docs: list[Path], code_text: str, code_words: set[str]) -> None:
    missing_paths: set[str] = set()
    missing_names: set[str] = set()
    checked = 0
    for path in docs:
        for number, line in _doc_lines(path):
            for span in SPAN.findall(line):
                where = f"{path.as_posix()}:{number}"
                if _looks_like_path(span):
                    checked += 1
                    if not _path_exists(span) and span not in code_text:
                        missing_paths.add(f"{where}  `{span}`")
                elif IDENTIFIER.match(span) and any(ch.isalpha() for ch in span):
                    checked += 1
                    parts = [part for part in span.removesuffix("()").split(".") if part]
                    absent = [part for part in parts if part not in code_words]
                    if absent:
                        missing_names.add(f"{where}  `{span}`  (not found: {', '.join(absent)})")
    print(f"# backticked spans checked: {checked}")
    print(f"\n## MISSING PATHS ({len(missing_paths)})")
    print("\n".join(sorted(missing_paths)) or "none")
    print(f"\n## MISSING NAMES ({len(missing_names)})")
    print("\n".join(sorted(missing_names)) or "none")


def _readme_entities(strings: dict[str, Any]) -> None:
    text = read_text(ROOT / "README.md") or ""
    section = text.split("## Sensor Entities", 1)[-1].split("## Statistic Entities", 1)[0]
    listed: set[str] = set()
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.lstrip().startswith("|") or not cells or set(cells[0]) <= {"-", " "}:
            continue
        name = cells[0]
        if name.lower() != "entity" and "_" not in name:
            listed.add(name.lower())
    names: set[str] = set()
    for platform in strings.get("entity", {}).values():
        for entry in platform.values():
            name = str(entry.get("name", ""))
            if name and "{" not in name:
                names.add(name.lower())
    print("\n## README ENTITIES")
    print("in README.md, not strings.json:", ", ".join(sorted(listed - names)) or "none")
    print("in strings.json, not README.md:", ", ".join(sorted(names - listed)) or "none")


def _translation_keys(strings: dict[str, Any], icons: dict[str, Any], code_text: str) -> None:
    print("\n## TRANSLATION KEYS")
    for platform, entries in sorted(strings.get("entity", {}).items()):
        with_icons = set(icons.get("entity", {}).get(platform, {}))
        no_icon = sorted(set(entries) - with_icons)
        stale_icon = sorted(with_icons - set(entries))
        unused = sorted(key for key in entries if f'"{key}"' not in code_text)
        print(f"entity.{platform} without an icon: {', '.join(no_icon) or 'none'}")
        print(f"entity.{platform} icons with no string: {', '.join(stale_icon) or 'none'}")
        print(f"entity.{platform} keys not quoted in code: {', '.join(unused) or 'none'}")
    for group in ("exceptions", "issues"):
        unused = sorted(key for key in strings.get(group, {}) if f'"{key}"' not in code_text)
        print(f"{group} keys not quoted in code: {', '.join(unused) or 'none'}")
    used = set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', code_text))
    known = {key for group in ("exceptions", "issues") for key in strings.get(group, {})} | {
        key for entries in strings.get("entity", {}).values() for key in entries
    }
    print(f"translation_key literals with no string: {', '.join(sorted(used - known)) or 'none'}")
    english = (ROOT / INTEGRATION / "translations/en.json").read_bytes()
    same = english == (ROOT / INTEGRATION / "strings.json").read_bytes()
    print(f"translations/en.json identical to strings.json: {'yes' if same else 'NO'}")


def _services(strings: dict[str, Any], icons: dict[str, Any]) -> None:
    yaml_text = read_text(ROOT / INTEGRATION / "services.yaml") or ""
    declared = set(re.findall(r"^([a-z_]+):", yaml_text, flags=re.MULTILINE))
    in_strings = set(strings.get("services", {}))
    in_icons = set(icons.get("services", {}))
    code = (read_text(ROOT / INTEGRATION / "services.py") or "") + (
        read_text(ROOT / INTEGRATION / "const.py") or ""
    )
    registered = {name for name in declared | in_strings if f'"{name}"' in code}
    print("\n## SERVICES")
    print(f"services.yaml: {', '.join(sorted(declared)) or 'none'}")
    print(f"strings.json: {', '.join(sorted(in_strings)) or 'none'}")
    print(f"icons.json: {', '.join(sorted(in_icons)) or 'none'}")
    print(f"quoted in services.py or const.py: {', '.join(sorted(registered)) or 'none'}")


def main() -> None:
    """Print every check's results."""
    files = repo_files()
    code_text = "\n".join(
        read_text(path) or ""
        for path in files
        if path.as_posix().startswith(CODE_ROOTS) or path.as_posix() in CODE_FILES
        if path.suffix != ".png"
    )
    code_words = set(WORD.findall(code_text))
    _check_spans(_doc_files(files), code_text, code_words)
    strings = json.loads((ROOT / INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / INTEGRATION / "icons.json").read_text(encoding="utf-8"))
    _readme_entities(strings)
    _translation_keys(strings, icons, code_text)
    _services(strings, icons)


if __name__ == "__main__":
    main()
