"""Evidence classification: real wheels, and no vendor names under conduit/src."""

from __future__ import annotations

import ast
import functools
import re
import sys
import tomllib
import typing
import zipfile
from pathlib import Path

import pytest

from conduit.outside_in.evidence import classify_distribution, spec_for_distribution

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[2]
_FIXTURE_WHEELS = REPO / "conduit" / "fixtures" / "impact" / "wheels"
_KNOWN_BREAKS = REPO / "docs" / "known-breaks.json"
_TOP_PYPI = Path(__file__).resolve().parent / "data" / "top-pypi-names.txt"
_GITHUB = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_IDENT = re.compile(r"[a-z0-9]+")


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


@functools.lru_cache(maxsize=1)
def _project_names() -> set[str]:
    """Dependencies this repository itself imports. Those names are not vendors."""
    payload = tomllib.loads((REPO / "conduit" / "pyproject.toml").read_text(encoding="utf-8"))
    found = {_norm(payload["project"]["name"])}
    for spec in payload["project"].get("dependencies") or []:
        found.add(_norm(re.split(r"[><=!~\s\[]", spec, maxsplit=1)[0]))
    for spec in (payload.get("build-system") or {}).get("requires") or []:
        found.add(_norm(re.split(r"[><=!~\s\[]", spec, maxsplit=1)[0]))
    for spec in (payload["project"].get("optional-dependencies") or {}).get("dev") or []:
        found.add(_norm(re.split(r"[><=!~\s\[]", spec, maxsplit=1)[0]))
    return found


def _needles(name: str) -> set[str]:
    text = name.strip().lower()
    found = {text}
    if "-" in text or "_" in text:
        found.add(text.replace("-", "_"))
        found.add(text.replace("_", "-"))
    return {item for item in found if len(item) >= 3}


@functools.lru_cache(maxsize=1)
def _metadata_blobs() -> list[str]:
    blobs: list[str] = []
    if not _FIXTURE_WHEELS.is_dir():
        return blobs
    for path in _FIXTURE_WHEELS.rglob("*"):
        if path.suffix == ".whl":
            try:
                with zipfile.ZipFile(path) as archive:
                    metas = [name for name in archive.namelist() if name.endswith("METADATA")]
                    if metas:
                        blobs.append(archive.read(metas[0]).decode("utf-8", "replace"))
            except (OSError, zipfile.BadZipFile):
                continue
        elif path.name.endswith(".tar.gz"):
            continue
    return blobs


@functools.lru_cache(maxsize=1)
def _fixture_names() -> frozenset[str]:
    raw: set[str] = set()
    for blob in _metadata_blobs():
        for line in blob.splitlines():
            if line.startswith("Name:"):
                raw.add(line.split(":", 1)[1].strip())
                break
        for org, repo in _GITHUB.findall(blob):
            raw.add(org)
            raw.add(repo)
    if _KNOWN_BREAKS.is_file():
        import json

        document = json.loads(_KNOWN_BREAKS.read_text(encoding="utf-8"))
        for repo in document.get("repos") or []:
            service = str(repo.get("service") or "").strip()
            if service:
                raw.add(service)
            pin = str(repo.get("pin") or "")
            package = re.split(r"[><=!~\s\[]", pin, maxsplit=1)[0]
            if package:
                raw.add(package)
    return frozenset(raw)


@functools.lru_cache(maxsize=1)
def _top_pypi_names() -> frozenset[str]:
    if not _TOP_PYPI.is_file():
        return frozenset()
    found: set[str] = set()
    for line in _TOP_PYPI.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            found.add(name)
    return frozenset(found)


@functools.lru_cache(maxsize=1)
def _schema_tokens() -> frozenset[str]:
    """Enum values the packet schema requires. Those labels are protocol, not vendors."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            enum = node.get("enum")
            if isinstance(enum, list):
                for item in enum:
                    if isinstance(item, str):
                        found.add(item.lower())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    import json

    schema_dir = REPO / "conduit" / "schema"
    if schema_dir.is_dir():
        for path in schema_dir.glob("*.json"):
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return frozenset(found)


@functools.lru_cache(maxsize=1)
def _vendor_names() -> dict[str, set[str]]:
    """Package and forge names taken from fixtures, the known-break file, and top PyPI.

    The list is not written by hand. Fixture and known-break names match as a
    substring, so an organization prefix matches a module named after the
    package. A short name matches only as a token. Top PyPI names match as a
    whole identifier, so a standard-library field that merely starts with the
    same letters is not a hit.
    """
    excluded = _project_names() | _schema_tokens()
    names: dict[str, set[str]] = {}
    for name in _fixture_names():
        if _norm(name) in excluded or name.lower() in excluded:
            continue
        needles = _needles(name)
        if needles:
            names[name.lower()] = needles
    for name in _top_pypi_names():
        if _norm(name) in excluded or name.lower() in excluded or name.lower() in names:
            continue
        needles = _needles(name)
        if needles:
            names[name.lower()] = needles
    return names


@functools.lru_cache(maxsize=1)
def _strict_names() -> frozenset[str]:
    """Names from fixtures and the known-break file. These match inside a longer token."""
    excluded = _project_names()
    return frozenset(name.lower() for name in _fixture_names() if _norm(name) not in excluded)


def _matches(blob: str, names: dict[str, set[str]]) -> list[str]:
    lowered = blob.lower()
    tokens = set(_IDENT.findall(lowered))
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    strict = _strict_names()
    found: list[str] = []
    for name, needles in names.items():
        for needle in needles:
            if name in strict or name not in {item.lower() for item in _top_pypi_names()}:
                if len(needle) <= 3:
                    if needle in tokens:
                        found.append(name)
                        break
                elif needle in lowered:
                    found.append(name)
                    break
            elif needle in words or needle.replace("-", "_") in words:
                found.append(name)
                break
    return found


def _scan_tree(root: Path, names: dict[str, set[str]]) -> list[str]:
    """Scan file names and source text (identifiers, comments, and strings)."""
    leaks: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for part in Path(rel).parts:
            for name in _matches(part, names):
                leaks.append(f"file {rel}: {name}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in _matches(text, names):
            leaks.append(f"source {rel}: {name}")
    return leaks


def test_classifier_sources_have_no_vendor_literals():
    """Vendor and package names fail the build anywhere under conduit/src.

    File names, module names, identifiers, comments, and strings are scanned
    against names taken from fixture metadata, the known-break packages, and
    the top PyPI snapshot. There is no hand-written deny list in src.
    """
    names = _vendor_names()
    assert names
    leaks = _scan_tree(SRC, names)
    assert leaks == []


def test_vendor_names_come_from_fixture_metadata():
    names = _vendor_names()
    saw_package = False
    saw_short_org = False
    for blob in _metadata_blobs():
        for line in blob.splitlines():
            if line.startswith("Name:"):
                package = line.split(":", 1)[1].strip().lower()
                if package in names:
                    saw_package = True
                break
        for org, _repo in _GITHUB.findall(blob):
            if len(org) <= 3 and org.lower() in names:
                saw_short_org = True
    assert saw_package
    assert saw_short_org


def test_boundary_scanner_rejects_the_old_layout(tmp_path: Path):
    """A module named for a fixture package, with a short forge token, fails."""
    names = _vendor_names()
    strict = _strict_names()
    longs = sorted(name for name in strict if len(name) >= 6)
    shorts = sorted(name for name in strict if len(name) <= 3)
    assert longs and shorts
    path = tmp_path / f"{longs[0]}.py"
    path.write_text(f'fallback = "{shorts[0]}"\n', encoding="utf-8")
    assert _scan_tree(tmp_path, names)


@pytest.mark.parametrize("name", ["pydantic", "pandas", "numpy", "attrs"])
def test_pure_compute_distributions_are_not_services(name: str):
    found = classify_distribution(name)
    assert found.loaded, name
    assert found.service is False
    assert found.transport is False


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("supabase", "api"),
        ("slack_sdk", "api"),
        ("psycopg2", "database"),
        ("stripe", "api"),
        ("boto3", "cloud"),
        ("sqlalchemy", "database"),
        ("openai", "api"),
    ],
)
def test_real_clients_classify_from_their_own_code(name: str, kind: str):
    found = classify_distribution(name)
    assert found.loaded, name
    assert found.service is True
    assert found.transport is False
    assert found.kind == kind
    hops = found.evidence.split(" -> ")
    assert hops[0] == name.replace("_", "-")
    assert hops[-1] in {"ssl", "socket", "http.client", "urllib.request", "db-api", "service-model"}


@pytest.mark.parametrize("version", ["3.13.0", "4.10.1"])
def test_wire_pool_cursor_driver_is_a_database(version: str):
    found = classify_distribution("pymongo", version)
    assert found.loaded, version
    assert found.service is True
    assert found.transport is False
    assert found.kind == "database"
    assert found.evidence.split(" -> ")[-1] in {"socket", "ssl", "db-api"}


def test_http_core_without_a_product_client_is_a_transport():
    core = classify_distribution("azure-core")
    assert core.loaded
    assert core.service is True
    assert core.transport is True
    assert spec_for_distribution("azure-core") is None
    product = classify_distribution("azure-storage-blob")
    assert product.loaded
    assert product.service is True
    assert product.transport is False
    assert product.kind == "api"
    assert spec_for_distribution("azure-storage-blob") is not None


@functools.lru_cache(maxsize=1)
def _stdlib_identifiers() -> frozenset[str]:
    """Identifiers the installed standard library already uses.

    A public API name that Python itself spells is not a fingerprint of one
    dependency. The set is read from the library, not typed out here.
    """
    root = Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    found: set[str] = set()
    if not root.is_dir():
        return frozenset()
    ident = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
    for path in root.rglob("*.py"):
        if any(part in {"site-packages", "test", "tests"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.update(ident.findall(text))
    return frozenset(found)


def _public_api_tokens() -> dict[str, set[str]]:
    """Class and method names from fixture wheels and the known-break files.

    A name is guarded when only one distribution exposes it, so a word shared
    by several public APIs (a method both of them spell the same way) is not
    treated as a vendor fingerprint. Builtins and typing wrappers are not
    either. The set is built from those files, not written out by hand.
    """
    by_dist: dict[str, set[str]] = {}

    def add(dist: str, name: str) -> None:
        if not dist or not name or not name.isidentifier() or name.startswith("_"):
            return
        by_dist.setdefault(dist.lower(), set()).add(name)

    def absorb_tree(dist: str, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                add(dist, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(dist, node.name)

    if _FIXTURE_WHEELS.is_dir():
        for path in _FIXTURE_WHEELS.rglob("*"):
            if path.suffix != ".whl":
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    metas = [name for name in archive.namelist() if name.endswith("METADATA")]
                    dist = ""
                    if metas:
                        for line in archive.read(metas[0]).decode("utf-8", "replace").splitlines():
                            if line.startswith("Name:"):
                                dist = line.split(":", 1)[1].strip()
                                break
                    if not dist:
                        continue
                    for name in archive.namelist():
                        if not name.endswith(".py") or name.endswith("/__init__.py") and name.count("/") > 4:
                            continue
                        if not name.endswith(".py"):
                            continue
                        if "/test" in name or name.startswith("test"):
                            continue
                        try:
                            source = archive.read(name).decode("utf-8", "replace")
                            absorb_tree(dist, ast.parse(source))
                        except (SyntaxError, UnicodeError, OSError):
                            continue
            except (OSError, zipfile.BadZipFile):
                continue
    import json

    for path in sorted((REPO / "docs").glob("known-breaks*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        repos = document.get("repos") if isinstance(document, dict) else None
        if not isinstance(repos, list):
            continue
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            dist = str(repo.get("service") or "").strip()
            expected = repo.get("expected")
            if not isinstance(expected, list):
                continue
            for item in expected:
                for part in str(item.get("symbol") or "").split("."):
                    add(dist, part)
    wrappers = {name.lower() for name in dir(typing) if name[:1].isalpha()}
    builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
    stdlib = _stdlib_identifiers()
    counts: dict[str, set[str]] = {}
    for dist, names in by_dist.items():
        for name in names:
            counts.setdefault(name, set()).add(dist)
    guarded: dict[str, set[str]] = {}
    for name, dists in counts.items():
        if len(dists) != 1:
            continue
        if name in builtins or name in stdlib or name.lower() in wrappers:
            continue
        interior = any(char.isupper() for char in name[1:])
        if not (
            ("_" in name and len(name) >= 5)
            or (interior and len(name) >= 8)
            or (name[:1].isupper() and len(name) >= 10)
            or len(name) >= 12
        ):
            continue
        guarded[name] = dists
    return guarded


def _api_leaks(root: Path, tokens: dict[str, set[str]]) -> list[str]:
    leaks: list[str] = []
    patterns = {name: re.compile(rf"\b{re.escape(name)}\b") for name in tokens}
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for name, pattern in patterns.items():
            if pattern.search(text) or pattern.search(path.name):
                leaks.append(f"{rel}: {name}")
    return leaks


def test_classifier_sources_have_no_public_api_names():
    """Class and method names from the eval and fixture APIs fail the build.

    The names are read from the wheels and the known-break files. A name that
    several distributions share is not specific enough to be a fingerprint.
    """
    tokens = _public_api_tokens()
    assert tokens
    assert _api_leaks(SRC, tokens) == []


def test_resolver_has_no_name_to_type_literals():
    """The resolver does not map names to types, except typing wrappers.

    The allowed names are the ones Python's ``typing`` module defines.
    """
    wrappers = {name.lower() for name in dir(typing) if name[:1].isalpha()}
    files = [
        SRC / "conduit" / "outside_in" / "library_types.py",
        SRC / "conduit" / "outside_in" / "graph.py",
    ]
    leaks: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and _dict_maps_names_to_types(node, wrappers):
                leaks.append(f"{path.name}:{node.lineno} dict")
            elif isinstance(node, ast.Set) and _set_of_type_names(node.elts, wrappers):
                leaks.append(f"{path.name}:{node.lineno} set")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset"
                and node.args
                and isinstance(node.args[0], ast.Set)
                and _set_of_type_names(node.args[0].elts, wrappers)
            ):
                leaks.append(f"{path.name}:{node.lineno} frozenset")
    assert leaks == []


def _type_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return bool(node.id[:1].isupper())
    if isinstance(node, ast.Attribute):
        return bool(node.attr[:1].isupper())
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
        if not text:
            return False
        return bool(re.match(r"[A-Z][A-Za-z0-9_]+(\.[A-Z][A-Za-z0-9_]+)*$", text))
    return False


def _dict_maps_names_to_types(node: ast.Dict, wrappers: set[str]) -> bool:
    if not node.keys or any(key is None for key in node.keys):
        return False
    if not all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in node.keys):
        return False
    keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
    if keys and all(str(key).lower() in wrappers for key in keys):
        return False
    if not node.values or not all(_type_like(value) for value in node.values):
        return False
    return True


def _set_of_type_names(elts: list[ast.expr], wrappers: set[str]) -> bool:
    if not elts or not all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in elts):
        return False
    values = [elt.value for elt in elts if isinstance(elt, ast.Constant)]
    if all(str(value).lower() in wrappers for value in values):
        return False
    return any(bool(re.match(r"[A-Z]", str(value))) for value in values)


def test_caller_methods_choose_the_client_they_use():
    found = classify_distribution("supabase")
    assert found.loaded
    assert found.service is True
    assert found.transport is False
    hops = found.evidence.split(" -> ")
    assert "postgrest" in hops
    assert hops[-1] in {"ssl", "socket", "http.client", "urllib.request", "db-api", "service-model"}
