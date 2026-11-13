"""Check the docs' code references and the integration's UI metadata against the code.

Sections, all sorted:

- MISSING PATHS: backticked paths in the docs (and this skill's SKILL.md) that don't exist. A
  glob such as `tests/fixtures/ecotricity/portal_*.json` must match at least one file.
- MISSING NAMES: backticked identifiers in the docs that appear nowhere in the code, tests,
  scripts, frontend or config, not even inside a longer name (so a suffix such as
  `_carbon_savings` that the code builds with an f-string still counts as found). Fixtures
  under tests/fixtures/ don't count: a name only Ecotricity's responses have isn't one the
  code reads, so a response field the docs name goes in known_external.txt.

Spans listed in known_external.txt (names from Home Assistant, libraries or Ecotricity's site,
and examples) are left out of both lists, so only new candidates print. Add a span there once
it's been checked; remove one if the docs stop mentioning it.
- README ENTITIES: entity names in README.md's entity tables that strings.json doesn't have,
  and strings.json entity names the README doesn't list. A row with a placeholder, such as
  `_Period_ import, export or controlled price`, lists every name it fits.
- TRANSLATION KEYS: strings.json keys with no icon, keys the code never uses, and keys the
  code uses that strings.json doesn't have, flow errors config_flow.py sets included (only
  that way round, as Home Assistant supplies some abort reasons); translations/en.json
  against strings.json. A key
  counts as used if a string literal in the integration's Python equals it, or an f-string
  there with a few letters of its own (such as f"current_{direction}_period") could build it.
- SERVICES: services.yaml, strings.json, icons.json and services.py against each other.
"""

from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING, Any

from _repo import ROOT, SKILL_DIR, read_text, repo_files

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
# Ecotricity's responses and registry data: a name found only here isn't in the code.
NOT_CODE = ("tests/fixtures/",)
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
    ".claude/skills/full-review/",
    ".claude/skills/full-review/scripts/",
)
PATH_SUFFIXES = (
    ".py",
    ".ts",
    ".js",
    ".json",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".png",
    ".lock",
    ".txt",
)
FENCE = re.compile(r"^\s*(```|~~~)")
SPAN = re.compile(r"`([^`\n]+)`")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(\(\))?$")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KNOWN_EXTERNAL = SKILL_DIR / "scripts/known_external.txt"
PLACEHOLDER = re.compile(r"_[a-z ]+_")
CHOICES = re.compile(r"[a-z-]+(?:, [a-z-]+)* or [a-z-]+")
# An f-string needs this many letters of its own to count as building a key, so f"{icp}_{key}"
# doesn't match every key there is.
TEMPLATE_LETTERS = 3


def _doc_files(files: list[Path]) -> list[Path]:
    docs = [
        path
        for path in files
        if path.as_posix() in DOCS or (path.as_posix().startswith("docs/") and path.suffix == ".md")
    ]
    return [*docs, SKILL_DIR.relative_to(ROOT) / "SKILL.md"]


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
    if "*" in candidate:
        return any(next((ROOT / prefix).glob(candidate), None) for prefix in PATH_PREFIXES)
    return any((ROOT / f"{prefix}{candidate}").exists() for prefix in PATH_PREFIXES)


def _looks_like_path(span: str) -> bool:
    if " " in span or any(mark in span for mark in "{}<>$:="):
        return False
    return "/" in span or span.endswith(PATH_SUFFIXES)


def _known_external() -> set[str]:
    lines = KNOWN_EXTERNAL.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def _check_spans(docs: list[Path], code_text: str, code_words: set[str]) -> None:
    missing_paths: set[str] = set()
    missing_names: set[str] = set()
    known = _known_external()
    used: set[str] = set()
    checked = 0
    for path in docs:
        for number, line in _doc_lines(path):
            for span in SPAN.findall(line):
                where = f"{path.as_posix()}:{number}"
                if span in known:
                    used.add(span)
                    continue
                if _looks_like_path(span):
                    checked += 1
                    if not _path_exists(span) and span not in code_text:
                        missing_paths.add(f"{where}  `{span}`")
                elif IDENTIFIER.match(span) and any(ch.isalpha() for ch in span):
                    checked += 1
                    parts = [part for part in span.removesuffix("()").split(".") if part]
                    absent = [part for part in parts if part not in code_words]
                    if absent and span.removesuffix("()").lstrip("_") not in code_text:
                        missing_names.add(f"{where}  `{span}`  (not found: {', '.join(absent)})")
    print(f"# backticked spans checked: {checked}, known external: {len(used)}")
    print(f"\n## MISSING PATHS ({len(missing_paths)})")
    print("\n".join(sorted(missing_paths)) or "none")
    print(f"\n## MISSING NAMES ({len(missing_names)})")
    print("\n".join(sorted(missing_names)) or "none")
    print(f"\n## STALE KNOWN EXTERNAL ({len(known - used)})")
    print("\n".join(sorted(known - used)) or "none")


def _row_pattern(name: str) -> re.Pattern[str]:
    """Return a placeholder row as a regex: "a, b or c" is any of them, `_Period_` any word."""
    pattern = CHOICES.sub(lambda m: f"(?:{'|'.join(re.split(', | or ', m[0]))})", name)
    return re.compile(PLACEHOLDER.sub(r"[\\w-]+", pattern))


def _readme_entities(strings: dict[str, Any]) -> None:
    text = read_text(ROOT / "README.md") or ""
    section = text.split("## Sensor Entities", 1)[-1].split("## Statistic Entities", 1)[0]
    listed: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.lstrip().startswith("|") or not cells or set(cells[0]) <= {"-", " "}:
            continue
        name = cells[0].lower()
        if PLACEHOLDER.search(name):
            patterns.append(_row_pattern(name))
        elif name != "entity":
            listed.add(name)
    names: set[str] = set()
    for platform in strings.get("entity", {}).values():
        for entry in platform.values():
            name = str(entry.get("name", ""))
            if name and "{" not in name:
                names.add(name.lower())
    unlisted = {name for name in names - listed if not any(p.fullmatch(name) for p in patterns)}
    print("\n## README ENTITIES")
    print("in README.md, not strings.json:", ", ".join(sorted(listed - names)) or "none")
    print("in strings.json, not README.md:", ", ".join(sorted(unlisted)) or "none")


def _python_strings(files: list[Path]) -> tuple[set[str], list[re.Pattern[str]]]:
    """Return the integration's string literals, and its f-strings as patterns."""
    literals: set[str] = set()
    templates: list[re.Pattern[str]] = []
    for path in files:
        if not path.as_posix().startswith(f"{INTEGRATION}/") or path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(read_text(path) or "")):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            elif isinstance(node, ast.JoinedStr):
                parts = [
                    re.escape(str(part.value)) if isinstance(part, ast.Constant) else "[a-z0-9_]+"
                    for part in node.values
                ]
                own = "".join(str(p.value) for p in node.values if isinstance(p, ast.Constant))
                if sum(char.isalpha() for char in own) >= TEMPLATE_LETTERS:
                    templates.append(re.compile("".join(parts)))
    return literals, templates


def _flow_errors() -> set[str]:
    """Return the error keys config_flow.py sets: `errors[x] = "key"` or `{"base": "key"}`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(read_text(ROOT / INTEGRATION / "config_flow.py") or "")):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "errors"
                for target in node.targets
            )
        ):
            found.add(node.value.value)
        # A field's error is keyed by its CONF_ constant, a form's by "base".
        elif isinstance(node, ast.Dict) and all(
            (isinstance(key, ast.Constant) and key.value == "base")
            or (isinstance(key, ast.Name) and key.id.startswith("CONF_"))
            for key in node.keys
        ):
            found.update(
                value.value
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
    return found


def _translation_keys(
    strings: dict[str, Any], icons: dict[str, Any], code_text: str, files: list[Path]
) -> None:
    literals, templates = _python_strings(files)

    def unused(keys: Any) -> str:  # noqa: ANN401 - a strings.json group
        found = [
            key
            for key in keys
            if key not in literals and not any(t.fullmatch(key) for t in templates)
        ]
        return ", ".join(sorted(found)) or "none"

    print("\n## TRANSLATION KEYS")
    for platform, entries in sorted(strings.get("entity", {}).items()):
        with_icons = set(icons.get("entity", {}).get(platform, {}))
        no_icon = sorted(set(entries) - with_icons)
        stale_icon = sorted(with_icons - set(entries))
        print(f"entity.{platform} without an icon: {', '.join(no_icon) or 'none'}")
        print(f"entity.{platform} icons with no string: {', '.join(stale_icon) or 'none'}")
        print(f"entity.{platform} keys not used in code: {unused(entries)}")
    for group in ("exceptions", "issues"):
        print(f"{group} keys not used in code: {unused(strings.get(group, {}))}")
    flow_errors = {
        key for group in ("config", "options") for key in strings.get(group, {}).get("error", {})
    }
    missing_errors = sorted(_flow_errors() - flow_errors)
    print(f"flow errors with no string: {', '.join(missing_errors) or 'none'}")
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
        if path.suffix != ".png" and not path.as_posix().startswith(NOT_CODE)
    )
    code_words = set(WORD.findall(code_text))
    _check_spans(_doc_files(files), code_text, code_words)
    strings = json.loads((ROOT / INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((ROOT / INTEGRATION / "icons.json").read_text(encoding="utf-8"))
    _readme_entities(strings)
    _translation_keys(strings, icons, code_text, files)
    _services(strings, icons)


if __name__ == "__main__":
    main()
