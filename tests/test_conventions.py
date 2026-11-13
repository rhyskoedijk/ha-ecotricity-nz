"""Rules from CLAUDE.md that no linter checks, but that can be checked without judgement.

Each test reads the code or the repository's config, so none of them needs updating as the code
grows: a new module, caller or file follows the same rule.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
import tomllib
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

import pytest
from PIL import Image

from custom_components.ecotricity_nz.api.parsers import parse_plan_list

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components/ecotricity_nz"
# Modules with no Home Assistant imports, directly or through what they import. api/ is also
# kept apart from the rest of the integration, so it can move to PyPI.
HOME_ASSISTANT_FREE = ("api", "tariff", "costs")
# CPU-bound functions that must only run in the executor.
EXECUTOR_ONLY = {"read_chart"}


def _module_path(parts: tuple[str, ...]) -> Path | None:
    for path in (
        PACKAGE.joinpath(*parts).with_suffix(".py"),
        PACKAGE.joinpath(*parts, "__init__.py"),
    ):
        if parts and path.is_file():
            return path
    return None


@cache
def _imports(path: Path) -> tuple[tuple[str, tuple[str, ...] | None], ...]:
    """Return each import as (module name, its parts within the package or None if outside)."""
    package = path.relative_to(PACKAGE).parent.parts
    found: list[tuple[str, tuple[str, ...] | None]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, _local(alias.name)) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = package[: len(package) - node.level + 1]
            parts = (*base, *(node.module or "").split(".")) if node.module else base
            found.append((".".join(parts), parts))
            # `from . import x` can import a module.
            found.extend(
                (".".join((*parts, alias.name)), (*parts, alias.name))
                for alias in node.names
                if _module_path((*parts, alias.name))
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, _local(node.module)))
    return tuple(found)


def _local(module: str) -> tuple[str, ...] | None:
    prefix = "custom_components.ecotricity_nz"
    if module == prefix or module.startswith(f"{prefix}."):
        return tuple(module.removeprefix(prefix).strip(".").split("."))
    return None


@pytest.mark.parametrize("seed", HOME_ASSISTANT_FREE)
def test_module_has_no_home_assistant_imports(seed: str) -> None:
    start = _module_path((seed,))
    assert start is not None
    roots = (
        [start, *sorted(PACKAGE.joinpath(seed).rglob("*.py"))]
        if start.name == "__init__.py"
        else [start]
    )
    seen: set[Path] = set()
    stack = [(path, [path.relative_to(PACKAGE).as_posix()]) for path in roots]
    broken: list[str] = []
    while stack:
        path, chain = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for module, parts in _imports(path):
            if module.split(".")[0] == "homeassistant":
                broken.append(f"{' -> '.join(chain)} imports {module}")
            if parts is None:
                continue
            if seed == "api" and parts[0] != "api":
                broken.append(f"{' -> '.join(chain)} imports {module}, outside api/")
            if (local := _module_path(parts)) is not None:
                stack.append((local, [*chain, local.relative_to(PACKAGE).as_posix()]))

    assert broken == []


def test_cpu_bound_functions_only_run_in_the_executor() -> None:
    misused: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        passed = {
            id(arg)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_add_executor_job"
            for arg in node.args[:1]
        }
        misused.extend(
            f"{path.relative_to(ROOT)}:{node.lineno} {node.id}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in EXECUTOR_ONLY and id(node) not in passed
        )

    assert misused == []


def test_version_is_left_for_cd_to_set() -> None:
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (manifest["version"], project["project"]["version"]) == ("0.0.0", "0.0.0")


def test_manifest_adds_no_requirements() -> None:
    # Pillow and aiohttp ship with Home Assistant. Change this only with a good reason.
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))

    assert manifest.get("requirements", []) == []


@pytest.mark.parametrize(
    "path",
    [".env", ".captures/capture.har", "custom_components/ecotricity_nz/frontend/card.js"],
)
def test_secrets_captures_and_the_built_card_are_git_ignored(path: str) -> None:
    result = subprocess.run(  # noqa: S603 - git with fixed arguments
        ["git", "check-ignore", "--quiet", "--no-index", path],  # noqa: S607 - git on PATH
        check=False,
        cwd=ROOT,
    )

    assert result.returncode == 0, f"{path} isn't git-ignored"


def _tracked_python() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "*.py"],  # noqa: S607 - git on PATH
        capture_output=True,
        check=True,
        cwd=ROOT,
    ).stdout.decode()
    return sorted(ROOT / name for name in output.split("\0") if name and (ROOT / name).is_file())


def _package_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(PACKAGE.rglob("*.py"))
    ]


# A suppression with its reason: Ruff's codes, a hyphen and the reason, or mypy's bracketed
# codes and then a second comment.
NOQA = re.compile(r"noqa\b(?!: [A-Z0-9]+(?:, ?[A-Z0-9]+)* - \S)")
TYPE_IGNORE = re.compile(r"type: ?ignore(?!\[[a-z-]+(?:, ?[a-z-]+)*\] +# \S)")


def test_every_suppression_gives_a_reason() -> None:
    unexplained: list[str] = []
    for path in _tracked_python():
        text = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            # Mypy's reason is the comment after its codes, on the same line.
            line = text.splitlines()[token.start[0] - 1][token.start[1] :]
            if NOQA.search(token.string) or TYPE_IGNORE.search(line):
                unexplained.append(f"{path.relative_to(ROOT)}:{token.start[0]} {token.string}")

    assert unexplained == []


# Prices stay in cents everywhere else (CLAUDE.md "Gotchas").
CONVERTS_CENTS = {"sensor.py", "costs.py"}


def _is_cents_conversion(node: ast.AST) -> bool:
    if not isinstance(node, ast.BinOp):
        return False
    right = node.right
    if isinstance(node.op, ast.Div):
        return (isinstance(right, ast.Constant) and right.value == 100) or (
            isinstance(right, ast.Name) and right.id == "CENTS_PER_DOLLAR"
        )
    return isinstance(node.op, ast.Mult) and any(
        isinstance(side, ast.Constant) and side.value == 0.01 for side in (node.left, right)
    )


def test_only_sensors_and_costs_convert_cents_to_dollars() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path, tree in _package_trees()
        if path.relative_to(PACKAGE).as_posix() not in CONVERTS_CENTS
        for node in ast.walk(tree)
        if _is_cents_conversion(node)
    ]

    assert found == []


def test_requests_verify_tls() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{node.lineno} {node.arg}"
        for path, tree in _package_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg in {"ssl", "verify_ssl"}
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
    ]

    assert found == []


def test_every_url_is_https_to_a_host_security_md_lists() -> None:
    security = (ROOT / "docs/security.md").read_text(encoding="utf-8")
    found: list[str] = []
    for path, tree in _package_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for url in re.findall(r"\w+://[^\s\"'`]+", node.value):
                parsed = urlparse(url)
                if parsed.scheme != "https" or f"`{parsed.hostname}`" not in security:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {url}")

    assert found == []


def _runtime_data_classes(trees: list[tuple[Path, ast.Module]]) -> set[str]:
    """Return the classes a `ConfigEntry[...]` alias holds, which Home Assistant fills in."""
    return {
        node.slice.id
        for _, tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ConfigEntry"
        and isinstance(node.slice, ast.Name)
    }


def test_models_are_frozen_dataclasses() -> None:
    trees = _package_trees()
    runtime_data = _runtime_data_classes(trees)
    mutable: list[str] = []
    for path, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                name = call.func if call else decorator
                if not (isinstance(name, ast.Name) and name.id == "dataclass"):
                    continue
                frozen = call is not None and any(
                    keyword.arg == "frozen"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
                if not frozen and node.name not in runtime_data:
                    mutable.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")

    assert mutable == []


def test_nothing_is_kept_in_hass_data_per_entry() -> None:
    found: list[str] = []
    for path, tree in _package_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt) or hasattr(node, "body"):
                continue
            source = ast.unparse(node)
            if re.search(r"\bhass\.data\b", source) and "entry_id" in source:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert found == []


@cache
def _published_names() -> frozenset[str]:
    """Return every fixture plan's ID and code, and every fixture chart's file name."""
    names: set[str] = set()
    for path in (ROOT / "tests/fixtures/networks").glob("*/*/plan_list.json"):
        for plan in parse_plan_list(json.loads(path.read_text(encoding="utf-8"))).all_plans:
            names.update((plan.id, plan.code))
    names.update(path.name for path in (ROOT / "tests/fixtures/networks").glob("*/*/charts/*"))
    return frozenset(names)


def test_tests_name_no_published_plan_or_chart() -> None:
    # They change whenever Ecotricity republishes, so tests find plans by role instead.
    published = _published_names()
    found = [
        f"{path.relative_to(ROOT)}:{node.lineno} {node.value}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (
            node.value in published
            or any(f"{name}" in node.value for name in published if name.endswith(".png"))
        )
    ]

    assert found == []


def test_every_fixture_outside_networks_is_loaded() -> None:
    # Network fixtures are found by walking their folders; every other one is named in a test.
    fixtures = ROOT / "tests/fixtures"
    tests = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests").rglob("*.py"))
    )
    unused = [
        path.relative_to(fixtures).as_posix()
        for path in sorted(fixtures.rglob("*"))
        if path.is_file()
        and not path.is_relative_to(fixtures / "networks")
        and path.name not in tests
    ]

    assert unused == []


def test_brand_images_keep_the_brands_sizes() -> None:
    # The sizes docs/development.md lists: a 256 px square icon, 128 px tall logos, @2x doubled.
    brand = PACKAGE / "brand"
    sizes = {path.name: Image.open(path).size for path in sorted(brand.glob("*.png"))}

    assert sizes.keys() == {
        f"{name}{scale}.png" for name in ("icon", "logo", "dark_logo") for scale in ("", "@2x")
    }
    for name in ("icon", "logo", "dark_logo"):
        width, height = sizes[f"{name}.png"]
        expected = (256, 256) if name == "icon" else (width, 128)
        assert (width, height) == expected
        assert sizes[f"{name}@2x.png"][1] == 2 * height


# A full commit SHA, then a comment naming the version (or the branch, for an action that
# publishes none) so Dependabot and a reader can tell what it is.
PINNED_USES = re.compile(r"uses: [\w./-]+@[0-9a-f]{40} # \S+")


def test_every_action_is_pinned_with_a_version_comment() -> None:
    found = [
        f"{path.name}:{number} {line.strip()}"
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "uses:" in line and not PINNED_USES.search(line)
    ]

    assert found == []
