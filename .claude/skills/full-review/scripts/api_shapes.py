"""Compare the shapes of Ecotricity responses with the keys the API client reads.

Reads every Ecotricity JSON fixture under tests/fixtures (and, when present, JSON and HAR
files under the git-ignored .captures/), then prints:

- SHAPES: each response kind's key paths and value types.
- KEYS READ BY THE CLIENT: string keys api/*.py looks up (`.get("x")`, `["x"]`, `"x" in`).
- KEYS IN RESPONSES, NEVER READ: per response kind.
- KEYS READ, IN NO RESPONSE: keys the client reads that no fixture or capture has.

Only key names and value types are printed, never values. Keys that look like identifiers
(four or more digits in a row, or an ICP shape) print as <id>, so captures can't leak them.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from _repo import ROOT, read_text, repo_files

if TYPE_CHECKING:
    from pathlib import Path

API_DIR = "custom_components/ecotricity_nz/api/"
FIXTURES = "tests/fixtures/"
# Not Ecotricity responses: the Electricity Authority's registry data.
NOT_ECOTRICITY = {"network_price_categories.json"}
CAPTURES = ROOT / ".captures"
DYNAMIC_KEY = re.compile(r"\d{4,}|^\d{10}[A-Za-z]{2}[0-9A-Za-z]{3}$")


def _key(name: str) -> str:
    return "<id>" if DYNAMIC_KEY.search(name) else name


def _walk(value: Any, path: str, shapes: dict[str, set[str]], keys: set[str]) -> None:  # noqa: ANN401 - walks arbitrary JSON
    shapes[path or "$"].add(type(value).__name__)
    if isinstance(value, dict):
        for name, child in value.items():
            key = _key(str(name))
            keys.add(key)
            _walk(child, f"{path}.{key}", shapes, keys)
    elif isinstance(value, list):
        for child in value:
            _walk(child, f"{path}[]", shapes, keys)


def _responses() -> list[tuple[str, Any]]:
    """Return (kind, parsed JSON) for every fixture and capture, sorted by kind."""
    found: list[tuple[str, Any]] = []
    for path in repo_files():
        posix = path.as_posix()
        if (
            posix.startswith(FIXTURES)
            and path.suffix == ".json"
            and path.name not in NOT_ECOTRICITY
        ):
            found.append((f"fixture {path.name}", json.loads(read_text(path) or "null")))
    if CAPTURES.is_dir():
        for path in sorted(CAPTURES.rglob("*")):
            if path.suffix == ".json":
                try:
                    found.append((f"capture {path.name}", json.loads(path.read_text("utf-8"))))
                except UnicodeDecodeError, json.JSONDecodeError:
                    continue
            elif path.suffix == ".har":
                found.extend(_har_responses(path))
    return sorted(found, key=lambda item: item[0])


def _har_responses(path: Path) -> list[tuple[str, Any]]:
    try:
        har = json.loads(path.read_text("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return []
    found: list[tuple[str, Any]] = []
    for entry in har.get("log", {}).get("entries", []):
        content = entry.get("response", {}).get("content", {})
        if "json" not in str(content.get("mimeType", "")):
            continue
        url = urlparse(str(entry.get("request", {}).get("url", "")))
        endpoint = "/".join(_key(part) for part in url.path.split("/"))
        try:
            found.append((f"capture {url.hostname}{endpoint}", json.loads(content.get("text", ""))))
        except TypeError, json.JSONDecodeError:
            continue
    return found


def _keys_read() -> dict[str, set[str]]:
    """Return each string key the client looks up, with the files that look it up."""
    read: dict[str, set[str]] = defaultdict(set)
    for path in repo_files():
        if not path.as_posix().startswith(API_DIR) or path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(read_text(path) or "")):
            key: object = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop", "setdefault"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                key = node.args[0].value
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                key = node.slice.value
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Constant)
                and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
            ):
                key = node.left.value
            if isinstance(key, str):
                read[key].add(path.name)
    return read


def main() -> None:
    """Print the shapes and both differences."""
    by_kind: dict[str, tuple[dict[str, set[str]], set[str]]] = {}
    for kind, payload in _responses():
        shapes, keys = by_kind.setdefault(kind, (defaultdict(set), set()))
        _walk(payload, "", shapes, keys)
    read = _keys_read()

    print("## SHAPES")
    for kind, (shapes, _) in sorted(by_kind.items()):
        print(f"\n### {kind}")
        for path, types in sorted(shapes.items()):
            print(f"{path}: {'|'.join(sorted(types))}")

    print(f"\n## KEYS READ BY THE CLIENT ({len(read)})")
    for key, files in sorted(read.items()):
        print(f"{key}  ({', '.join(sorted(files))})")

    print("\n## KEYS IN RESPONSES, NEVER READ")
    for kind, (_, keys) in sorted(by_kind.items()):
        unread = sorted(keys - set(read))
        print(f"{kind}: {', '.join(unread) or 'none'}")

    seen = set().union(*(keys for _, keys in by_kind.values())) if by_kind else set()
    missing = sorted(set(read) - seen)
    print(f"\n## KEYS READ, IN NO RESPONSE ({len(missing)})")
    print("\n".join(f"{key}  ({', '.join(sorted(read[key]))})" for key in missing) or "none")


if __name__ == "__main__":
    main()
