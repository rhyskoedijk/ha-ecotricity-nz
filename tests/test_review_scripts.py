"""The full-review skill's scripts still run, and their clear-cut checks pass.

Sections with one right answer (no missing final newlines, no writing rule breaks, every
scenario tagged, every name the docs mention still in the code and so on) are checked here, so
they're caught on every PR rather than at the next full review. A name the docs mention from
outside the repo goes in known_external.txt, and a response key the client ignores on purpose
in ignored_keys.txt. Sections that need judgement, such as unreferenced
symbols, are left to the review.
"""

from __future__ import annotations

import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / ".claude/skills/full-review/scripts"


@cache
def _output(script: str) -> str:
    result = subprocess.run(  # noqa: S603 - runs the repo's own scripts with this interpreter
        [sys.executable, SCRIPTS / script],
        capture_output=True,
        check=False,
        cwd=SCRIPTS.parents[3],
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _section(output: str, heading: str) -> list[str]:
    """Return the lines under a `## heading`, up to the next heading."""
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"## {heading}")), None)
    assert start is not None, f"no `## {heading}` heading in the output"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line:
            body.append(line)
    return body


@pytest.mark.parametrize(
    "script",
    sorted(path.name for path in SCRIPTS.glob("*.py") if not path.name.startswith("_")),
)
def test_review_script_runs(script: str) -> None:
    assert _output(script).startswith("#")


@pytest.mark.parametrize(
    ("script", "heading"),
    [
        ("newlines.py", "NO FINAL NEWLINE"),
        ("prose.py", "WRITING RULES"),
        ("doc_refs.py", "MISSING PATHS"),
        ("doc_refs.py", "MISSING NAMES"),
        ("doc_refs.py", "STALE KNOWN EXTERNAL"),
        ("api_shapes.py", "KEYS IN RESPONSES, NEVER READ"),
        ("api_shapes.py", "STALE IGNORED KEYS"),
        ("api_shapes.py", "KEYS READ, IN NO RESPONSE"),
        ("pii_scan.py", "CANDIDATES"),
        ("pii_scan.py", "STALE REVIEWED"),
        ("review_tags.py", "UNTAGGED"),
        ("review_tags.py", "UNKNOWN TAGS"),
        ("review_tags.py", "TAGS NOT ATTACHED"),
        ("symbols.py", "UNUSED TYPESCRIPT EXPORTS"),
        ("decisions.py", "INVALID DECISIONS"),
    ],
)
def test_review_section_is_empty(script: str, heading: str) -> None:
    output = _output(script)

    assert f"## {heading} (0)" in output, "\n".join(_section(output, heading))


@pytest.mark.parametrize("heading", ["README ENTITIES", "TRANSLATION KEYS"])
def test_ui_metadata_matches(heading: str) -> None:
    lines = _section(_output("doc_refs.py"), heading)

    assert lines
    assert [line for line in lines if not line.endswith((": none", ": yes"))] == []


def test_services_are_declared_everywhere() -> None:
    lines = _section(_output("doc_refs.py"), "SERVICES")

    assert len({line.split(": ", 1)[1] for line in lines}) == 1, lines


@pytest.mark.parametrize(
    ("body", "breaks"),
    [
        ("It was wrong, so this fixes it.", False),
        ("It was wrong \u2014 this fixes it.", True),
        ("Fixes the color of the total.", True),
        ("# git's own comment \u2014 left out", False),
        ("It was wrong \u2014 see below.\n\nSigned-off-by: dependabot[bot] <x>", False),
    ],
)
def test_commit_message_writing_rules(tmp_path: Path, body: str, *, breaks: bool) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(f"fix: correct the total\n\n{body}\n", encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - runs the repo's own script with this interpreter
        [sys.executable, SCRIPTS / "prose.py", "--message", message],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert (result.returncode != 0) is breaks, result.stdout
