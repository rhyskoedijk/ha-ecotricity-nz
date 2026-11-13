"""Check the docs, UI text, code and commit messages against CLAUDE.md's writing rules.

Reads every Markdown file outside code fences, inline code and link targets, the string
values of strings.json, and every line of every Python and TypeScript file (code never needs
an em dash or emoji, so its comments, docstrings and UI text are checked without picking them
out). Prints each line with an em dash, a spaced en dash, a double hyphen used as punctuation
or an emoji, each Markdown or strings.json line with a US spelling from US_SPELLINGS (not
code, whose CSS and libraries say "color"), and each Markdown line that continues a paragraph
or list item from the line above (a hard wrap), as `file:line  rule  text`, sorted. Only
spellings with a clear NZ form are listed, so the list rarely needs adding to.

With `--message FILE`, it checks a commit message instead, leaving out git's `#` comment
lines, and exits with an error if any line breaks a rule (the `commit-writing-rules` hook).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from _repo import read_text, repo_files

STRINGS = "custom_components/ecotricity_nz/strings.json"
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`|\]\([^)]*\)")
CODE_SUFFIXES = {".py", ".ts"}
RULES = {
    "em dash": re.compile("\\u2014"),
    "en dash": re.compile("\\s\\u2013\\s"),
    "double hyphen": re.compile(r"(?<=\w) -- (?=\w)|(?<=\w)--(?=\s)"),
    "emoji": re.compile("[\\U0001f300-\\U0001faff\\u2600-\\u27bf]"),
}
US_SPELLINGS = re.compile(
    r"(?i)\b(colors?|behaviors?|favorites?|centers?|centered|labeled|labeling|canceled|"
    r"canceling|traveled|analyz\w*|organiz\w*|recogniz\w*|catalog|defense|licenses?)\b"
)


# Lines that start a block of their own, so they never continue the line above.
BLOCK_START = re.compile(r"^\s*(?:#|\||>|<|[-*+] |\d+[.)] |\[[^\]]+\]:|---|===|```|~~~|$)")
# A line that can't be continued: a heading, table row, HTML, rule, or a deliberate break.
UNCONTINUED = re.compile(r"^\s*(?:#|\||<|---|===|\[[^\]]+\]:)|(?: {2}|\\)$")


def _hard_wraps(text: str) -> list[tuple[int, str]]:
    """Return each line that continues the paragraph or list item on the line before."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    fenced = False
    # YAML front matter isn't Markdown.
    start = lines.index("---", 1) + 1 if lines[:1] == ["---"] and "---" in lines[1:] else 0
    for number in range(start, len(lines)):
        line = lines[number]
        if FENCE.match(line):
            fenced = not fenced
            continue
        previous = lines[number - 1] if number > start else ""
        if fenced or BLOCK_START.match(line) or not previous.strip():
            continue
        if not UNCONTINUED.search(previous) and not FENCE.match(previous):
            found.append((number + 1, line))
    return found


def _markdown(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append((number, INLINE_CODE.sub("", line)))
    return lines


def _strings(text: str) -> list[tuple[int, str]]:
    values: set[str] = set()

    def walk(value: Any) -> None:  # noqa: ANN401 - walks arbitrary JSON
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, str):
            values.add(value)

    walk(json.loads(text))
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if any(value in line for value in values)
    ]


def _check(path: Path, lines: list[tuple[int, str]], *, spelling: bool) -> list[str]:
    rules = {**RULES, "US spelling": US_SPELLINGS} if spelling else RULES
    return [
        f"{path.as_posix()}:{number}  {rule}  {line.strip()[:120]}"
        for number, line in lines
        for rule, pattern in rules.items()
        if pattern.search(line)
    ]


def _check_message(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.startswith("#")
    ]
    if found := _check(path, lines, spelling=True):
        print("\n".join(found))
        sys.exit(1)


def main() -> None:
    """Print every line that breaks a writing rule, or check a commit message."""
    if sys.argv[1:2] == ["--message"]:
        _check_message(Path(sys.argv[2]))
        return
    found: list[str] = []
    for path in repo_files():
        posix = path.as_posix()
        text = read_text(path) or ""
        if path.suffix == ".md":
            found.extend(_check(path, _markdown(text), spelling=True))
            found.extend(
                f"{posix}:{number}  hard wrap  {line.strip()[:120]}"
                for number, line in _hard_wraps(text)
            )
        elif path.suffix in CODE_SUFFIXES:
            lines = list(enumerate(text.splitlines(), start=1))
            found.extend(_check(path, lines, spelling=False))
        elif posix == STRINGS:
            found.extend(_check(path, _strings(text), spelling=True))
    print(f"## WRITING RULES ({len(found)})")
    print("\n".join(sorted(found)) or "none")


if __name__ == "__main__":
    main()
