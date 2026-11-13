"""List Python symbols that nothing references, or that only tests reference.

Inventories every module-level and class-level function, class, constant and field in
custom_components/, scripts/ and tests/conftest.py, then counts whole-word references to each
name across the code, tests, scripts and the frontend. Names called implicitly are left out:
Home Assistant's integration hooks (MODULE_HOOKS), class members that override a base class's
(found by importing the integration's modules, so new Home Assistant hooks need no list),
pytest hooks and autouse fixtures. Output is sorted so two runs on the same tree are identical.

UNUSED TYPESCRIPT EXPORTS lists each `export` in frontend/src that no other file in
frontend/src or frontend/test names (DEAD-04): tsc's unused checks don't cover exports.

These are candidates, not findings: a name can be used through getattr, a string or a
framework, so each one still has to be checked by hand.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from _repo import ROOT, read_text, repo_files

if TYPE_CHECKING:
    from pathlib import Path

DEFINITION_ROOTS = ("custom_components/", "scripts/", "tests/conftest.py")
REFERENCE_ROOTS = ("custom_components/", "scripts/", "tests/", "frontend/src/", "frontend/test/")
REFERENCE_SUFFIXES = {".py", ".ts", ".json", ".yaml", ".yml"}
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TS_EXPORT = re.compile(
    r"^export (?:default )?(?:async )?(?:function\*?|const|let|class|interface|type|enum) (\w+)",
    re.MULTILINE,
)

# Module-level names Home Assistant's loader, Python or a script runner calls by name.
MODULE_HOOKS = {
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_remove_entry",
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_get_config_entry_diagnostics",
    "main",
    "DOMAIN",
    "PLATFORMS",
    "CONFIG_SCHEMA",
    "PARALLEL_UPDATES",
}
IMPLICIT_PREFIXES = ("async_step_", "__", "_attr_", "pytest_")
# Config flow classes are registered by their domain= class keyword, not by name.
IMPLICIT_SUFFIXES = ("ConfigFlow",)


@dataclass(frozen=True)
class Symbol:
    """A defined name and where it's defined."""

    name: str
    path: Path
    line: int
    kind: str
    owner: str | None = None


def _implicit(name: str) -> bool:
    return (
        name in MODULE_HOOKS
        or name.startswith(IMPLICIT_PREFIXES)
        or name.endswith(IMPLICIT_SUFFIXES)
    )


def _autouse(node: ast.stmt) -> bool:
    """Return whether a function is a pytest fixture with autouse=True."""
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    return any(
        isinstance(decorator, ast.Call)
        and any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in node.decorator_list
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
        if _autouse(node):
            continue
        symbols.extend(Symbol(name, path, node.lineno, kind) for name, kind in _names(node))
        if isinstance(node, ast.ClassDef):
            symbols.extend(
                Symbol(name, path, member.lineno, f"class {kind}", node.name)
                for member in node.body
                for name, kind in _names(member)
            )
    return [symbol for symbol in symbols if not _implicit(symbol.name)]


def _overrides(symbols: list[Symbol]) -> tuple[set[Symbol], list[str]]:
    """Return the integration's class members that override a base class's, and import errors.

    Only custom_components/ is imported: scripts can have side effects, and tests need pytest.
    """
    sys.path.insert(0, str(ROOT))
    overriding: set[Symbol] = set()
    errors: set[str] = set()
    for symbol in symbols:
        if symbol.owner is None or symbol.path.parts[0] != "custom_components":
            continue
        module_name = ".".join(symbol.path.with_suffix("").parts)
        try:
            owner = getattr(importlib.import_module(module_name), symbol.owner)
        except Exception as err:  # noqa: BLE001 - reported, and the symbol is still checked
            errors.add(f"{module_name}.{symbol.owner}: {type(err).__name__}")
            continue
        if any(symbol.name in vars(base) for base in owner.__mro__[1:]):
            overriding.add(symbol)
    return overriding, sorted(errors)


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


def _word_counts(corpus: dict[Path, list[str]]) -> dict[str, Counter[str]]:
    """Count every whole word in code and in tests."""
    counts: dict[str, Counter[str]] = {"code": Counter(), "tests": Counter()}
    for path, lines in corpus.items():
        area = "tests" if path.as_posix().startswith("tests/") else "code"
        for line in lines:
            counts[area].update(WORD.findall(line))
    return counts


def _references(
    symbol: Symbol, corpus: dict[Path, list[str]], counts: dict[str, Counter[str]]
) -> dict[str, int]:
    """Count a symbol's references in code and in tests, leaving out its own definition."""
    where = {area: counts[area][symbol.name] for area in counts}
    lines = corpus.get(symbol.path, [])
    if symbol.line <= len(lines) and symbol.name in WORD.findall(lines[symbol.line - 1]):
        where["tests" if symbol.path.parts[0] == "tests" else "code"] -= 1
    return {area: count for area, count in where.items() if count > 0}


def _unused_exports(corpus: dict[Path, list[str]]) -> list[str]:
    """Return the frontend/src exports no other frontend file names."""
    frontend = {
        path: set(WORD.findall("\n".join(lines)))
        for path, lines in corpus.items()
        if path.as_posix().startswith(("frontend/src/", "frontend/test/"))
    }
    unused: list[str] = []
    for path, lines in sorted(corpus.items()):
        if not path.as_posix().startswith("frontend/src/") or path.suffix != ".ts":
            continue
        text = "\n".join(lines)
        for match in TS_EXPORT.finditer(text):
            name = match[1]
            if not any(name in words for other, words in frontend.items() if other != path):
                line = text.count("\n", 0, match.start()) + 1
                unused.append(f"{path.as_posix()}:{line}  {name}")
    return unused


def main() -> None:
    """Print the unreferenced and test-only symbols, and unused TypeScript exports."""
    symbols, corpus = _load()
    counts = _word_counts(corpus)
    overriding, errors = _overrides(symbols)
    symbols = [symbol for symbol in symbols if symbol not in overriding]
    unreferenced: list[str] = []
    test_only: list[str] = []
    for symbol in sorted(symbols, key=lambda s: (s.path.as_posix(), s.line, s.name)):
        where = _references(symbol, corpus, counts)
        label = f"{symbol.path.as_posix()}:{symbol.line}  {symbol.kind}  {symbol.name}"
        if not where:
            unreferenced.append(label)
        elif "code" not in where and symbol.path.parts[0] != "tests":
            test_only.append(f"{label}  (tests: {where['tests']})")

    print(f"# symbols scanned: {len(symbols)}, base class overrides left out: {len(overriding)}")
    print(f"# classes that couldn't be imported: {', '.join(errors) or 'none'}")
    print(f"\n## UNREFERENCED ({len(unreferenced)})")
    print("\n".join(unreferenced) or "none")
    print(f"\n## REFERENCED ONLY BY TESTS ({len(test_only)})")
    print("\n".join(test_only) or "none")
    exports = _unused_exports(corpus)
    print(f"\n## UNUSED TYPESCRIPT EXPORTS ({len(exports)})")
    print("\n".join(exports) or "none")


if __name__ == "__main__":
    main()
