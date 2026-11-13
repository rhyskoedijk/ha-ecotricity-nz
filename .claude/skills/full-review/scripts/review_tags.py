"""List the tests tagged with each check in SKILL.md.

Python tests are tagged `@pytest.mark.review("TEST-02", ...)` and frontend tests with a
`// review: TEST-21` comment on the line above the `describe` or `it`. Any check ID can be a
tag (a kept probe for PRICE-02 is tagged PRICE-02), but only the `- **TEST-NN**` rows with no
title are scenarios that must have one (checks with a title, such as TEST-01, need no tag).

- TAGGED: each check with tagged tests, and the tests.
- UNTAGGED: TEST scenarios no test is tagged with.
- UNKNOWN TAGS: tags naming a check SKILL.md doesn't have.
- TAGS NOT ATTACHED: tags this script couldn't tie to a test, such as a frontend tag that
  isn't directly above a `describe`, `it` or `test`, or a Python marker that isn't a test's
  decorator. Their tests aren't counted under TAGGED, so fix the tag.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict

from _repo import SKILL_DIR, read_text, repo_files

CHECK = re.compile(r"^- \*\*([A-Z]+-\d+)\b", re.MULTILINE)
SCENARIO = re.compile(r"^- \*\*(TEST-\d+)\*\* ", re.MULTILINE)
PY_MARKER = re.compile(r"pytest\.mark\.review\(")
TS_TAG = re.compile(r"^\s*// review: ([A-Z0-9, -]+)$")
TS_CALL = re.compile(r"^\s*(?:describe|it|test)(?:\.\w+)*\(\s*(?:([\"'`])(.*?)\1)?")
CHECK_ID = re.compile(r"[A-Z]+-\d+")


def _python_tags(text: str) -> tuple[list[tuple[str, str]], int]:
    """Return (check ID, test name) for each tag, and how many markers weren't decorators."""
    found: list[tuple[str, str]] = []
    attached = 0
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == (
                "pytest.mark.review"
            ):
                attached += 1
                found.extend(
                    (arg.value, node.name)
                    for arg in decorator.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                )
    return found, len(PY_MARKER.findall(text)) - attached


def _frontend_tags(text: str, posix: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (check ID, test name) for each tag, and where each unattached tag is."""
    found: list[tuple[str, str]] = []
    unattached: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        tag = TS_TAG.match(line)
        if tag is None:
            continue
        call = TS_CALL.match(lines[index + 1]) if index + 1 < len(lines) else None
        if call is None:
            unattached.append(f"{posix}:{index + 1}")
            continue
        name = call[2] or f"line {index + 2}"
        found.extend((check, name) for check in CHECK_ID.findall(tag[1]))
    return found, unattached


def main() -> None:
    """Print the tagged, untagged, unknown and unattached tags."""
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    checks = CHECK.findall(skill)
    scenarios = SCENARIO.findall(skill)
    tagged: dict[str, set[str]] = defaultdict(set)
    unattached: list[str] = []
    for path in repo_files():
        posix = path.as_posix()
        text = read_text(path) or ""
        if posix.startswith("tests/") and path.suffix == ".py":
            found, missed = _python_tags(text)
            if missed:
                unattached.append(f"{posix}: {missed} marker(s) not on a test")
        elif posix.startswith("frontend/test/") and path.suffix == ".ts":
            found, where = _frontend_tags(text, posix)
            unattached.extend(where)
        else:
            continue
        for check, name in found:
            tagged[check].add(f"{posix}::{name}")

    known = [check for check in checks if check in tagged]
    print(f"## TAGGED ({len(known)} checks, {len(set(scenarios) & set(tagged))} of ", end="")
    print(f"{len(scenarios)} scenarios)")
    print("\n".join(f"{check}: {', '.join(sorted(tagged[check]))}" for check in known) or "none")
    untagged = [check for check in scenarios if check not in tagged]
    print(f"\n## UNTAGGED ({len(untagged)})")
    print("\n".join(untagged) or "none")
    unknown = sorted(set(tagged) - set(checks))
    print(f"\n## UNKNOWN TAGS ({len(unknown)})")
    print("\n".join(f"{check}: {', '.join(sorted(tagged[check]))}" for check in unknown) or "none")
    print(f"\n## TAGS NOT ATTACHED ({len(unattached)})")
    print("\n".join(sorted(unattached)) or "none")


if __name__ == "__main__":
    main()
