"""List Python symbols that nothing references, or that only tests reference.

Inventories every module-level and class-level function, class, constant and field in
custom_components/ and scripts/, then counts whole-word references to each name across
the code, tests, scripts and the frontend. Names Home Assistant or Python call implicitly
are left out (HA_ENTRY_POINTS). Output is sorted so two runs on the same tree are identical.

These are candidates, not findings: a name can be used through getattr, a string or a
framework, so each one still has to be checked by hand.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from _repo import read_text, repo_files

if TYPE_CHECKING:
    from pathlib import Path

DEFINITION_ROOTS = ("custom_components/", "scripts/")
REFERENCE_ROOTS = ("custom_components/", "scripts/", "tests/", "frontend/src/", "frontend/test/")
REFERENCE_SUFFIXES = {".py", ".ts", ".json", ".yaml", ".yml"}

# Called by Home Assistant, Python or a framework rather than by this repo's code.
HA_ENTRY_POINTS = {
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_remove_entry",
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_get_config_entry_diagnostics",
    "async_get_options_flow",
    "async_added_to_hass",
    "async_will_remove_from_hass",
    "async_update",
    "async_image",
    "async_shutdown",
    "_async_update_data",
    "_async_setup",
    "_async_migrate_func",
    "native_value",
    "native_unit_of_measurement",
    "extra_state_attributes",
    "available",
    "options",
    "image_url",
    "image_last_updated",
    "device_info",
    "name",
    "translation_placeholders",
    "unique_id",
    "is_on",
    "main",
    "VERSION",
    "MINOR_VERSION",
    "DOMAIN",
    "PLATFORMS",
    "CONFIG_SCHEMA",
    "PARALLEL_UPDATES",
}
HA_ENTRY_PREFIXES = ("async_step_", "__", "_attr_")
# Config flow classes are registered by their domain= class keyword, not by name.
HA_ENTRY_SUFFIXES = ("ConfigFlow",)


@dataclass(frozen=True)
class Symbol:
    """A defined name and where it's defined."""

    name: str
    path: Path
    line: int
    kind: str


def _implicit(name: str) -> bool:
    return (
        name in HA_ENTRY_POINTS
        or name.startswith(HA_ENTRY_PREFIXES)
        or name.endswith(HA_ENTRY_SUFFIXES)
    )


def _names(node: ast.stmt) -> list[tuple[str, str]]:
    """Return the (name, kind) pairs a statement defines."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return [(node.name, "function")]
    if isinstance(node, ast.ClassDef):
        return [(node.name, "class")]
    if isinstance(node, ast.Assign):
        return [(t.id, "constant") for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [(node.target.id, "field")]
    return []


def _defined(path: Path, tree: ast.Module) -> list[Symbol]:
    """Return a module's top-level and class-level symbols."""
    symbols: list[Symbol] = []
    for node in tree.body:
        members = node.body if isinstance(node, ast.ClassDef) else []
        symbols.extend(Symbol(name, path, node.lineno, kind) for name, kind in _names(node))
        for member in members:
            symbols.extend(
                Symbol(name, path, member.lineno, f"class {kind}") for name, kind in _names(member)
            )
    return [symbol for symbol in symbols if not _implicit(symbol.name)]


def _re_export_lines(tree: ast.Module) -> set[int]:
    """Return the lines of a package's imports and __all__, which only re-export names."""
    lines: set[int] = set()
    for node in tree.body:
        is_all = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        )
        if isinstance(node, ast.ImportFrom) or is_all:
            end = node.end_lineno or node.lineno
            lines.update(range(node.lineno, end + 1))
    return lines


def _load() -> tuple[list[Symbol], dict[Path, list[str]]]:
    """Return every defined symbol, and the lines of every file that can reference one."""
    symbols: list[Symbol] = []
    corpus: dict[Path, list[str]] = {}
    for path in repo_files():
        text = read_text(path)
        if text is None:
            continue
        posix = path.as_posix()
        tree = ast.parse(text) if path.suffix == ".py" else None
        if tree is not None and posix.startswith(DEFINITION_ROOTS):
            symbols.extend(_defined(path, tree))
        if not posix.startswith(REFERENCE_ROOTS) or path.suffix not in REFERENCE_SUFFIXES:
            continue
        lines = text.splitlines()
        if tree is not None and path.name == "__init__.py":
            skipped = _re_export_lines(tree)
            lines = ["" if n in skipped else line for n, line in enumerate(lines, start=1)]
        corpus[path] = lines
    return symbols, corpus


def _references(symbol: Symbol, corpus: dict[Path, list[str]]) -> dict[str, int]:
    """Count a symbol's references in code and in tests, leaving out its own definition."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol.name)}(?![A-Za-z0-9_])")
    where: dict[str, int] = defaultdict(int)
    for path, lines in corpus.items():
        area = "tests" if path.as_posix().startswith("tests/") else "code"
        for number, line in enumerate(lines, start=1):
            count = len(pattern.findall(line))
            if path == symbol.path and number == symbol.line:
                count -= 1
            if count > 0:
                where[area] += count
    return where


def main() -> None:
    """Print the unreferenced and test-only symbols."""
    symbols, corpus = _load()
    unreferenced: list[str] = []
    test_only: list[str] = []
    for symbol in sorted(symbols, key=lambda s: (s.path.as_posix(), s.line, s.name)):
        where = _references(symbol, corpus)
        label = f"{symbol.path.as_posix()}:{symbol.line}  {symbol.kind}  {symbol.name}"
        if not where:
            unreferenced.append(label)
        elif "code" not in where:
            test_only.append(f"{label}  (tests: {where['tests']})")

    print(f"# symbols scanned: {len(symbols)}")
    print(f"\n## UNREFERENCED ({len(unreferenced)})")
    print("\n".join(unreferenced) or "none")
    print(f"\n## REFERENCED ONLY BY TESTS ({len(test_only)})")
    print("\n".join(test_only) or "none")


if __name__ == "__main__":
    main()
