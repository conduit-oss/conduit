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

from conduit.outside_in.evidence import _Code, _scan, classify_distribution, spec_for_distribution

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[2]
_FIXTURE_WHEELS = REPO / "conduit" / "fixtures" / "impact" / "wheels"
_KNOWN_BREAKS = REPO / "docs" / "archive" / "known-breaks.json"
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


def test_pool_is_opening_a_connection_and_lending_it_back():
    """A pool opens a socket, takes one off a collection, and puts one back.

    Two method names side by side are not that behavior.
    """
    lending = _scan(
        _Code(
            package="zzpool",
            version="0",
            files=[
                (
                    "zzpool.py",
                    b"""
import socket

class Lane:
    def __init__(self):
        self.items = []

    def open_one(self):
        return socket.create_connection(("127.0.0.1", 9))

    def borrow(self):
        try:
            return self.items.pop()
        except IndexError:
            return self.open_one()

    def give(self, item):
        self.items.append(item)
""",
                )
            ],
            requires=[],
            modules=("zzpool",),
        )
    )
    named = _scan(
        _Code(
            package="zzpool",
            version="0",
            files=[
                (
                    "zzpool.py",
                    b"""
import socket

class Lane:
    def get_socket(self):
        return socket.create_connection(("127.0.0.1", 9))

    def connect(self):
        return self.get_socket()
""",
                )
            ],
            requires=[],
            modules=("zzpool",),
        )
    )
    assert lending.pool is True
    assert named.pool is False


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


def _wheel_dist_name(archive: zipfile.ZipFile) -> str:
    metas = [name for name in archive.namelist() if name.endswith("METADATA") or name.endswith("PKG-INFO")]
    for meta in metas:
        for line in archive.read(meta).decode("utf-8", "replace").splitlines():
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip()
    return ""


def _absorb_python(dist: str, source: str, by_dist: dict[str, set[str]]) -> None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return
    for node in ast.walk(tree):
        name = ""
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        if not name or not name.isidentifier() or name.startswith("_"):
            continue
        by_dist.setdefault(dist.lower(), set()).add(name)


def _absorb_wheel_bytes(dist: str, filename: str, blob: bytes, by_dist: dict[str, set[str]]) -> None:
    if not filename.endswith(".py"):
        return
    lowered = filename.lower()
    if any(part in {"test", "tests", "testing"} for part in lowered.split("/")):
        return
    _absorb_python(dist, blob.decode("utf-8", "replace"), by_dist)


def _pin_version(dist: str, pin: str) -> str:
    """Concrete version named by a pin, or the highest release the pin allows."""
    exact = re.search(r"==\s*([^\s,;]+)", pin or "")
    if exact:
        return exact.group(1).strip()
    spec = re.sub(r"^[A-Za-z0-9_.\-]+\s*", "", (pin or "").split(";", 1)[0]).strip()
    if not spec:
        return ""
    from conduit.impact.versions import highest_on_pypi

    try:
        return highest_on_pypi(dist, spec)
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def _stdlib_common_identifiers() -> frozenset[str]:
    """Identifiers the standard library uses as shared vocabulary.

    A name two or more library modules spell is vocabulary, and so is a name
    any module uses as something other than the single place it is defined.
    A method one module defines and never otherwise mentions stays out of
    this set. ``telnetlib.Telnet.get_socket`` is that case: the same method
    on a third-party pool is still a fingerprint of that distribution.
    """
    root = Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if not root.is_dir():
        root = Path("/usr/lib") / f"python{sys.version_info.major}.{sys.version_info.minor}"
    defined: dict[str, int] = {}
    used: dict[str, int] = {}
    files: dict[str, int] = {}
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
        spelled = {name for name in set(ident.findall(text)) if not name.startswith("_")}
        for name in spelled:
            files[name] = files.get(name, 0) + 1
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        defs: set[str] = set()
        uses: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name.isidentifier() and not node.name.startswith("_"):
                    defs.add(node.name)
            elif isinstance(node, ast.Name) and node.id.isidentifier() and not node.id.startswith("_"):
                uses.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr.isidentifier() and not node.attr.startswith("_"):
                uses.add(node.attr)
            elif isinstance(node, ast.arg) and node.arg.isidentifier() and not node.arg.startswith("_"):
                uses.add(node.arg)
        for name in defs:
            defined[name] = defined.get(name, 0) + 1
        for name in uses:
            used[name] = used.get(name, 0) + 1
    common: set[str] = set()
    for name in set(files) | set(defined) | set(used):
        # Spelled in several modules: ordinary vocabulary, not one vendor's method.
        if files.get(name, 0) >= 2 or used.get(name, 0) >= 1 or defined.get(name, 0) >= 2:
            common.add(name)
    return frozenset(common)


@functools.lru_cache(maxsize=1)
def _public_api_tokens() -> frozenset[str]:
    """Public class, method, and function names from fixture and eval wheels.

    Every real wheel under the impact fixtures is read, and so is each
    distribution pinned by a known-break file. A name is guarded when only
    one distribution defines it. Builtins, the standard library, and typing
    wrappers are not. Expected-symbol lists are not the source: a method the
    eval never mentions is still a fingerprint when the wheel defines it.
    """
    import json

    from conduit.outside_in.evidence import _download_code
    from conduit.outside_in.specs import normalize_dist

    by_dist: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    if _FIXTURE_WHEELS.is_dir():
        for path in sorted(_FIXTURE_WHEELS.rglob("*.whl")):
            try:
                with zipfile.ZipFile(path) as archive:
                    dist = _wheel_dist_name(archive)
                    if not dist:
                        continue
                    key = (dist.lower(), path.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    for name in archive.namelist():
                        _absorb_wheel_bytes(dist, name, archive.read(name), by_dist)
            except (OSError, zipfile.BadZipFile):
                continue
    docs_root = REPO / "docs"
    known_break_paths = list(docs_root.glob("known-breaks*.json"))
    archive = docs_root / "archive"
    if archive.is_dir():
        known_break_paths.extend(archive.glob("known-breaks*.json"))
    for path in sorted(known_break_paths):
        # Mined / scored dumps grow without bound and are not classifier
        # fixtures; including them dilutes single-distribution fingerprints.
        if path.name.endswith("-results.json") or "-mined" in path.name:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        repos = document.get("repos") if isinstance(document, dict) else None
        if not isinstance(repos, list):
            continue
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            dist = str(repo.get("service") or "").strip()
            version = _pin_version(dist, str(repo.get("pin") or ""))
            if not dist or not version:
                continue
            key = (dist.lower(), version)
            if key in seen:
                continue
            seen.add(key)
            try:
                code = _download_code(normalize_dist(dist), version)
            except Exception:
                code = None
            if code is None:
                continue
            for filename, blob in code.files:
                _absorb_wheel_bytes(dist, filename, blob, by_dist)
    wrappers = {name.lower() for name in dir(typing) if name[:1].isalpha()}
    builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
    stdlib = _stdlib_common_identifiers()
    counts: dict[str, set[str]] = {}
    for dist, names in by_dist.items():
        for name in names:
            counts.setdefault(name, set()).add(dist)
    guarded: set[str] = set()
    for name, dists in counts.items():
        if len(dists) != 1:
            continue
        if name in builtins or name.lower() in wrappers or name in stdlib:
            continue
        guarded.add(name)
    return frozenset(guarded)


_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _literal_api_names(value: str) -> set[str]:
    """Identifier tokens a string uses as a name, not words inside a sentence."""
    text = value.strip()
    if text.isidentifier():
        return {text}
    if _DOTTED.fullmatch(text):
        return set(text.split("."))
    return set()


def _bound_api_leaks(root: Path, tokens: set[str] | frozenset[str]) -> list[str]:
    """Public names used as string literals, attributes, or identifiers.

    A comment or a docstring sentence can use the same word. Those are not
    a string whose value is the name, and they are not an identifier.
    """
    leaks: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in tokens:
                found.add(node.attr)
            elif isinstance(node, ast.Name) and node.id in tokens:
                found.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in tokens:
                found.add(node.name)
            elif isinstance(node, ast.arg) and node.arg in tokens:
                found.add(node.arg)
            elif isinstance(node, ast.alias):
                if node.name in tokens:
                    found.add(node.name)
                if node.asname in tokens:
                    found.add(node.asname)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(name for name in _literal_api_names(node.value) if name in tokens)
        rel = path.relative_to(root).as_posix()
        for name in sorted(found):
            leaks.append(f"{rel}: {name}")
    return leaks


def _collect_targets(nodes: list[ast.AST], found: set[str]) -> None:
    for node in nodes:
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.Tuple, ast.List)):
            _collect_targets(list(node.elts), found)
        elif isinstance(node, ast.Starred):
            _collect_targets([node.value], found)


def _defined_names(root: Path) -> set[str]:
    """Names this tree binds: functions, classes, parameters, and assignments."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(node.name)
            elif isinstance(node, ast.arg):
                found.add(node.arg)
            elif isinstance(node, ast.Assign):
                _collect_targets(list(node.targets), found)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                _collect_targets([node.target], found)
            elif isinstance(node, ast.NamedExpr):
                _collect_targets([node.target], found)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                _collect_targets([node.target], found)
            elif isinstance(node, ast.comprehension):
                _collect_targets([node.target], found)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                _collect_targets([node.optional_vars], found)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                found.add(node.name)
            elif isinstance(node, ast.alias):
                found.add((node.asname or node.name).split(".")[0])
    return found


def test_classifier_sources_have_no_public_api_names():
    """Class and method names from fixture and eval wheels fail the build.

    The names are read from the wheels, not from the expected-symbol lists.
    A name that several distributions share is not specific enough to be a
    fingerprint, and neither is a name the standard library already uses.
    A guarded name fails as a string literal, an attribute, or an identifier.
    A name this tree itself binds is this project's own API.
    """
    tokens = _public_api_tokens()
    assert tokens, "public names from the wheels have to be guarded"
    assert "get_socket" in tokens
    # Coined on one distribution and absent from the expected-symbol lists.
    assert "api_base" in tokens
    assert "request_stream" in tokens
    # ``connect`` is a method several distributions define, so it is not a fingerprint.
    assert "connect" not in tokens
    defined = _defined_names(SRC)
    leaks = [item for item in _bound_api_leaks(SRC, tokens) if item.split(": ", 1)[1] not in defined]
    assert leaks == []


def test_old_get_socket_check_is_a_public_api_leak(tmp_path: Path):
    """The removed method-name pair would fail this guard."""
    path = tmp_path / "evidence.py"
    path.write_text('if {"get_socket", "connect"} <= methods:\n    pass\n', encoding="utf-8")
    leaks = _bound_api_leaks(tmp_path, _public_api_tokens())
    assert "evidence.py: get_socket" in leaks


def test_api_names_match_literals_attributes_and_identifiers_only(tmp_path: Path):
    """``count`` as ``.count``, ``"count"``, or ``def count`` fails. Prose does not."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "attr.py").write_text("x = obj.count\n", encoding="utf-8")
    (package / "literal.py").write_text("name = 'count'\n", encoding="utf-8")
    (package / "ident.py").write_text("def count():\n    return 1\n", encoding="utf-8")
    (package / "prose.py").write_text(
        '"""count the rows"""\n# count is a word\n',
        encoding="utf-8",
    )
    leaks = _bound_api_leaks(package, {"count"})
    assert leaks == ["attr.py: count", "ident.py: count", "literal.py: count"]


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
