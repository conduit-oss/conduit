"""Decide whether a distribution is an outside service from its own code.

A distribution counts only when a wheel, an sdist, or installed files show
I/O evidence: a client, session, or connection whose construction leads to
the network, a DB-API 2.0 connection, or a versioned API spec. Dependencies
are followed a bounded number of levels. A helper that only downloads a URL
is not a client. Nothing here is a list of product names.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from conduit.outside_in.specs import ServiceSpec, normalize_dist

_SCHEMA = 27
_MAX_DEPTH = 4
_MAX_BYTES = 40_000_000
# Verb names on an HTTP client. ``close`` is not one: connection objects
# close without being HTTP libraries. ``options`` is not one either: drivers
# name a method that and do not speak HTTP.
_HTTP_SURFACE = frozenset(
    {
        "build_request",
        "delete",
        "get",
        "head",
        "patch",
        "post",
        "put",
        "request",
        "send",
        "stream",
        "urlopen",
    }
)
_DB_NAMES = frozenset({"commit", "connect", "cursor", "execute"})
_STRONG_TAILS = frozenset(
    {
        "HTTPConnection",
        "HTTPSConnection",
        "SSLContext",
        "create_connection",
        "create_default_context",
        "socket",
        "wrap_socket",
    }
)
_WEAK_TAILS = frozenset({"urlopen", "urlretrieve"})
_STDLIB_IO = {
    "http.client": "http.client",
    "httplib": "http.client",
    "socket": "socket",
    "ssl": "ssl",
    "urllib.request": "urllib.request",
}
_SKIP_PARTS = frozenset(
    {
        "benchmarks",
        "docs",
        "example",
        "examples",
        "scripts",
        "test",
        "testing",
        "tests",
    }
)
_RESERVED_TLDS = frozenset({"example", "invalid", "localhost", "test"})
_STOP_TOKENS = frozenset(
    {
        "api",
        "app",
        "callback",
        "data",
        "event",
        "events",
        "hook",
        "hooks",
        "http",
        "https",
        "incoming",
        "object",
        "post",
        "request",
        "signature",
        "type",
        "v1",
        "v2",
        "v3",
        "webhook",
    }
)
_MEMORY: dict[tuple[str, str], "Classification"] = {}


@dataclass(frozen=True)
class Classification:
    name: str
    version: str = ""
    package: str = ""
    service: bool = False
    kind: str = ""
    transport: bool = False
    constructors: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    host_mode: str = "exact"
    modules: tuple[str, ...] = ()
    evidence: str = ""
    loaded: bool = False
    #: Services this package reaches only by calling them. Empty when it does the I/O itself.
    delegates: tuple[str, ...] = ()


@dataclass
class _Direct:
    package: str = ""
    version: str = ""
    modules: tuple[str, ...] = ()
    strong: bool = False
    stdlib: str = ""
    database: bool = False
    spec: str = ""
    constructors: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    public_io: set[str] = field(default_factory=set)
    used_roots: set[str] = field(default_factory=set)
    requires: list[str] = field(default_factory=list)
    factories: set[str] = field(default_factory=set)
    domain_classes: set[str] = field(default_factory=set)
    domain_candidates: dict[str, set[str]] = field(default_factory=dict)
    domain_strong: set[str] = field(default_factory=set)
    http_surface: bool = False
    constructed_roots: set[str] = field(default_factory=set)
    #: Public Client/Session classes that are not HTTP verb surfaces.
    client_classes: set[str] = field(default_factory=set)
    #: DB-API names are present. Wire-protocol drivers set ``database`` without this.
    db_api: bool = False
    #: The C or header sources define the DB-API names, so this package speaks the protocol.
    db_api_c: bool = False
    #: Method tails reachable from a DB-API method via ``self`` calls, including across modules.
    db_reach: set[str] = field(default_factory=set)
    #: Method tail -> external module roots that tail's body calls.
    deps_by_tail: dict[str, set[str]] = field(default_factory=dict)
    #: External roots reached from DB-API methods. The package is a wrapper when one of these is the service.
    delegate_roots: set[str] = field(default_factory=set)
    #: Tails of functions that themselves open a socket or TLS context.
    strong_tails: set[str] = field(default_factory=set)
    #: A DB-API method, or a method it reaches, is what opens the socket.
    db_strong: bool = False
    #: struct pack/unpack beside a socket, a pooled socket, and a cursor.
    wire: bool = False
    pool: bool = False
    cursor: bool = False
    #: Dependency root -> how many public caller methods reach a constructor of it.
    caller_hits: dict[str, int] = field(default_factory=dict)


def cache_dir() -> Path:
    from conduit.analysis_cache import cache_root, cache_version

    path = cache_root() / "classify" / cache_version()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _concrete_version(version: str | None) -> str:
    """A single wheel version. Requirement ranges are not one."""
    text = (version or "").strip()
    if not text or re.search(r"[,<>~*\s]", text):
        return ""
    return text


def classify_distribution(
    name: str,
    version: str | None = None,
    *,
    root: Path | None = None,
    depth: int = 0,
    stack: frozenset[str] | None = None,
) -> Classification:
    """Classify one distribution. Cached per normalized name and version."""
    norm = normalize_dist(name)
    if not norm or norm == "python":
        return Classification(name=norm)
    seen = stack or frozenset()
    if norm in seen or depth > _MAX_DEPTH:
        return Classification(name=norm)
    pinned = _concrete_version(version)
    if pinned:
        hit = _cached(norm, pinned)
        if hit is not None:
            return hit
    local = _local_code(norm, pinned, root)
    if local is not None and (not pinned or not local.version or local.version == pinned):
        resolved = local.version or pinned
        code = local
    else:
        resolved = pinned or _pypi_version(norm) or ""
        if resolved:
            hit = _cached(norm, resolved)
            if hit is not None:
                return hit
        code = _local_code(norm, resolved, root) if resolved else None
        if code is None and resolved:
            code = _download_code(norm, resolved)
    if not resolved and code is not None:
        resolved = code.version
    if resolved:
        hit = _cached(norm, resolved)
        if hit is not None:
            return hit
    if code is not None and code.source == "fixture":
        downloaded = _download_code(norm, code.version or resolved)
        if downloaded is not None:
            code = downloaded
            resolved = code.version or resolved
    if code is None:
        return Classification(name=norm, version=resolved)
    direct = _scan(code)
    result = _finish(norm, direct, root=root, depth=depth, stack=seen | {norm})
    key = (norm, result.version or resolved or "unresolved")
    _MEMORY[key] = result
    if result.loaded:
        _write_disk(key, result)
    return result


def spec_for_distribution(
    name: str,
    version: str | None = None,
    *,
    root: Path | None = None,
) -> ServiceSpec | None:
    """Service spec for a product client. Generic HTTP transports are not one."""
    found = classify_distribution(name, version, root=root)
    if not found.service or found.transport:
        return None
    return _as_spec(found)


def specs_for_repo(root: Path) -> tuple[ServiceSpec, ...]:
    """Services the repo imports or receives a webhook from, plus HTTP transports."""
    from conduit.deps.manifests import read_installed
    from conduit.outside_in.distributions import declared_distribution_names, is_stdlib_module

    root = root.resolve()
    pins = {normalize_dist(name): version for name, version in read_installed(root).items()}
    declared = {normalize_dist(name): name for name in declared_distribution_names(root)}
    index = _venv_import_index(root)
    specs: list[ServiceSpec] = []
    seen: set[str] = set()

    def add(name: str, version: str | None, *, module: str = "") -> None:
        norm = normalize_dist(name)
        if not norm or norm in seen or norm == "python" or is_stdlib_module(norm.replace("-", "_")):
            return
        if module and _is_local_module(root, module.split(".", 1)[0]):
            return
        seen.add(norm)
        found = classify_distribution(norm, version or pins.get(norm) or None, root=root)
        if module and found.loaded and not _provides(found, module):
            seen.discard(norm)
            alt = normalize_dist(module.replace(".", "-"))
            if alt and alt not in seen and alt != norm:
                add(alt, pins.get(alt), module=module)
            return
        if not found.service:
            # The import is a wrapper. The service is the dependency its
            # own functions call, when the caller's code reaches that call.
            for delegate in found.delegates:
                add(delegate, pins.get(normalize_dist(delegate)))
            return
        spec = _as_spec(found)
        if spec.service_id not in {item.service_id for item in specs}:
            specs.append(spec)

    for module in _imported_modules(root):
        top = module.split(".", 1)[0]
        if is_stdlib_module(top) or _is_local_module(root, top):
            continue
        dist = index.get(top) or declared.get(normalize_dist(top)) or ""
        if dist:
            add(dist, pins.get(normalize_dist(dist)), module=module)
            continue
        if module.count(".") >= 2:
            add(module.replace(".", "-"), pins.get(normalize_dist(module.replace(".", "-"))), module=module)
        else:
            add(top.replace("_", "-"), pins.get(normalize_dist(top)), module=module)
    for token in _webhook_tokens(root):
        add(token, pins.get(normalize_dist(token)))
    return tuple(specs)


def _as_spec(found: Classification) -> ServiceSpec:
    modules = found.modules or ((found.package or found.name).replace("-", "_"),)
    return ServiceSpec(
        service_id=found.name,
        package=found.name,
        modules=modules,
        hosts=found.hosts,
        host_mode=found.host_mode,
        constructor_tails=found.constructors,
        kind=found.kind or "api",
        evidence=found.evidence,
        transport=found.transport,
    )


def _provides(found: Classification, module: str) -> bool:
    if not module:
        return True
    for name in found.modules:
        if module == name or module.startswith(name + ".") or name.startswith(module + "."):
            return True
    return False


def _finish(
    norm: str,
    direct: _Direct,
    *,
    root: Path | None,
    depth: int,
    stack: frozenset[str],
) -> Classification:
    """Product clients name a host or construct a non-HTTP client.

    A package whose public surface is HTTP verbs, and that only reaches the
    network through those verbs, is a transport. A download helper that never
    builds a client is not a service.
    """
    used = _used_services(direct, root=root, depth=depth, stack=stack)
    _promote_domain(direct, used)
    if direct.database and not _own_io(direct):
        # Names like connect/cursor are not I/O when those functions only call
        # another dependency the classifier already counts. The service is that
        # dependency. A package that opens the socket, frames a protocol, or
        # implements the calls in C stays the service.
        delegated = _delegate_services(direct, root=root, depth=depth, stack=stack)
        drivers = [item for item in delegated if item.kind == "database" and not item.transport]
        if drivers:
            return Classification(
                name=norm,
                version=direct.version,
                package=direct.package or norm,
                service=False,
                modules=direct.modules,
                evidence=f"{norm} -> {drivers[0].evidence}",
                delegates=tuple(dict.fromkeys(item.name for item in drivers)),
                loaded=True,
            )
    products = [item for item in used if not item.transport]
    transports = [item for item in used if item.transport]
    cloud = next((item for item in products if item.kind == "cloud"), None)
    database_dep = next((item for item in products if item.kind == "database"), None)
    via = _evidence_dep(direct, products, transports)
    kind = ""
    evidence = ""
    transport = False
    if direct.database:
        kind = "database"
        if direct.db_api or not direct.stdlib:
            evidence = f"{norm} -> db-api"
        else:
            evidence = f"{norm} -> {direct.stdlib}"
    elif direct.spec == "service-model" or cloud is not None:
        kind = "cloud"
        evidence = f"{norm} -> service-model" if direct.spec == "service-model" else f"{norm} -> {cloud.evidence}"
    elif direct.hosts or direct.domain_classes:
        if database_dep is not None and not direct.strong and not direct.hosts:
            kind = "database"
            evidence = f"{norm} -> {database_dep.evidence}"
        else:
            kind = "api"
            if via is not None:
                evidence = f"{norm} -> {via.evidence}"
            elif direct.stdlib:
                evidence = f"{norm} -> {direct.stdlib}"
            elif direct.hosts:
                # The host was read from this package. The HTTP library it
                # uses was not attributed, and the host is still the evidence.
                evidence = f"{norm} -> {sorted(direct.hosts)[0]}"
    elif direct.http_surface and (direct.strong or transports):
        kind = "api"
        transport = True
        if transports:
            evidence = f"{norm} -> {transports[0].evidence}"
        elif direct.stdlib:
            evidence = f"{norm} -> {direct.stdlib}"
    if not kind or not evidence:
        return Classification(
            name=norm,
            version=direct.version,
            package=direct.package or norm,
            modules=direct.modules,
            loaded=True,
        )
    constructors = set(direct.constructors)
    if kind == "cloud":
        constructors.update(name for name in direct.factories if name in {"client", "resource"})
    if kind == "database":
        constructors.update(name for name in direct.factories if name in _DB_NAMES)
    return Classification(
        name=norm,
        version=direct.version,
        package=direct.package or norm,
        service=True,
        kind=kind,
        transport=transport,
        constructors=tuple(sorted(constructors)),
        hosts=tuple(sorted(direct.hosts)),
        modules=direct.modules,
        evidence=evidence,
        loaded=True,
    )


def _own_io(direct: _Direct) -> bool:
    """True when this package's database methods reach the network themselves.

    Calling another distribution does not count. A socket opened by those
    methods, a framed pool and cursor, a C implementation of the DB-API, a
    declared host, or a service model does. A socket in some other function
    does not.
    """
    return bool(
        direct.db_strong
        or (direct.wire and direct.pool and direct.cursor)
        or direct.db_api_c
        or direct.hosts
        or direct.spec == "service-model"
    )


def _delegate_services(
    direct: _Direct,
    *,
    root: Path | None,
    depth: int,
    stack: frozenset[str],
) -> list[Classification]:
    """Classify the external roots DB-API methods actually call.

    These are not limited to declared requirements. A backend the package
    imports only when that engine is selected is still the dependency the
    method reaches.
    """
    found: list[Classification] = []
    seen: set[str] = set()
    if depth >= _MAX_DEPTH:
        return found
    for dep in sorted(direct.delegate_roots):
        norm = normalize_dist(dep)
        if not norm or norm in seen or norm in stack:
            continue
        seen.add(norm)
        sub = classify_distribution(dep, root=root, depth=depth + 1, stack=stack)
        if sub.service:
            found.append(sub)
    return found


def cached_classification(name: str) -> Classification | None:
    """A classification this process already computed. Does not fetch."""
    norm = normalize_dist(name)
    if not norm:
        return None
    for (key_name, _version), found in _MEMORY.items():
        if key_name == norm and found.loaded:
            return found
    return None


def _promote_domain(direct: _Direct, used: list[Classification]) -> None:
    """A client class builds another service, or opens the socket itself.

    Calls that only decode, render, or annotate do not count. HTTP-verb
    classes are transports and were left out of the candidate set.
    """
    found: set[str] = set()
    for class_name, roots in direct.domain_candidates.items():
        # A class whose constructor builds another service is a product.
        # Names ending in Transport are the HTTP library's own adapter.
        if class_name.endswith("Transport"):
            continue
        if any(_root_overlap(roots, item) for item in used):
            found.add(class_name)
            direct.constructors.add(class_name)
    # Opening a socket in a client constructor is product evidence. The same
    # shape inside a package that already speaks HTTP verbs is the transport.
    if direct.domain_strong and not direct.http_surface:
        found.update(direct.domain_strong)
    # A Client or Session in a package that opens a socket is the object the
    # caller constructs. HTTP verb classes stay transports.
    if direct.strong and direct.client_classes:
        found.update(direct.client_classes)
        direct.constructors.update(direct.client_classes)
    direct.domain_classes = found


def _used_services(
    direct: _Direct,
    *,
    root: Path | None,
    depth: int,
    stack: frozenset[str],
) -> list[Classification]:
    found: list[Classification] = []
    if depth >= _MAX_DEPTH:
        return found
    seen: set[str] = set()
    for dep in direct.requires:
        norm = normalize_dist(dep)
        if not norm or norm in seen or not _uses_dep(direct, dep):
            continue
        sub = classify_distribution(dep, root=root, depth=depth + 1, stack=stack)
        if not sub.service or not _uses_dep(direct, dep, sub):
            continue
        seen.add(norm)
        found.append(sub)
    return found


def _evidence_dep(
    direct: _Direct,
    products: list[Classification],
    transports: list[Classification],
) -> Classification | None:
    """Prefer the dependency the caller's methods actually use.

    A client constructed only in ``__init__`` loses to one reached from the
    public methods those calls go through.
    """
    ordered = [*products, *transports]
    if not ordered:
        return None
    if direct.caller_hits:
        ranked = sorted(direct.caller_hits.items(), key=lambda item: (-item[1], item[0]))
        for root, _count in ranked:
            matched = [
                item
                for item in ordered
                if _uses_dep(direct, item.name, item) and _root_overlap({root}, item)
            ]
            if matched:
                return matched[0]
    if direct.constructed_roots:
        constructed = [
            item
            for item in ordered
            if _uses_dep(direct, item.name, item) and _root_overlap(direct.constructed_roots, item)
        ]
        built = [item for item in constructed if not item.transport]
        if built:
            return built[0]
        if constructed:
            return constructed[0]
    return ordered[0]


def _root_overlap(roots: set[str], found: Classification) -> bool:
    names = {normalize_dist(found.name), normalize_dist(found.package)}
    names.update(normalize_dist(module.split(".", 1)[0]) for module in found.modules)
    folded = {name.replace("-", "_") for name in names}
    for root in roots:
        token = normalize_dist(root)
        if token in names or token.replace("-", "_") in folded or root in folded:
            return True
    return False


def _uses_dep(direct: _Direct, dep: str, sub: Classification | None = None) -> bool:
    norm = normalize_dist(dep)
    roots = {norm, norm.replace("-", "_"), norm.replace("-", ".")}
    if sub is not None:
        roots.update(sub.modules)
        roots.update(name.split(".", 1)[0] for name in sub.modules)
    return any(root in direct.used_roots or root.replace("-", "_") in direct.used_roots for root in roots)


@dataclass
class _Code:
    package: str
    version: str
    files: list[tuple[str, bytes]]
    requires: list[str]
    modules: tuple[str, ...]
    #: installed, cache, fixture, or pypi. Fixture wheels are partial API extracts.
    source: str = ""


def _scan(code: _Code) -> _Direct:
    direct = _Direct(package=code.package, version=code.version, modules=code.modules, requires=list(code.requires))
    own = set(code.modules)
    c_db: set[str] = set()
    py_db: set[str] = set()
    for name, blob in code.files:
        if name.endswith((".c", ".h")):
            text = blob.decode("utf-8", "replace")
            for token in _DB_NAMES:
                if re.search(rf"\b{token}\b", text):
                    c_db.add(token)
            continue
        if name.endswith((".json", ".json.gz", ".yaml", ".yml")):
            kind = _spec_kind(name, blob)
            if kind == "service-model" or (kind and direct.spec != "service-model"):
                direct.spec = kind or direct.spec
            continue
        if not name.endswith(".py"):
            continue
        _scan_python(name, blob, direct, py_db, own)
    db_defs = c_db | py_db
    if _DB_NAMES <= c_db:
        direct.db_api_c = True
    if _DB_NAMES <= db_defs:
        direct.database = True
        direct.db_api = True
        direct.constructors.update(name for name in db_defs if name in direct.factories or name == "connect")
        direct.constructors.add("connect")
    elif direct.strong and direct.wire and direct.pool and direct.cursor:
        # A driver that frames bytes on a socket, pools connections, and
        # walks rows with a cursor is a database even without DB-API names.
        direct.database = True
        direct.constructors.update(direct.client_classes)
    for tail, deps in direct.deps_by_tail.items():
        if tail in direct.db_reach:
            direct.delegate_roots.update(deps)
    # A socket somewhere else in the distribution (a mailer, a dev server)
    # is not the database. Only a DB-API method that reaches that socket is.
    if direct.db_reach & direct.strong_tails:
        direct.db_strong = True
    return direct


def _is_our_module(module: str, modules: set[str]) -> bool:
    """True when this distribution ships ``module``.

    A one-label namespace does not make a sibling distribution under that
    namespace internal. Dotted packages this distribution contains do.
    """
    if not module or not modules:
        return False
    dotted = {name for name in modules if "." in name}
    considered = dotted or modules
    for ours in considered:
        if module == ours or module.startswith(ours + ".") or ours.startswith(module + "."):
            return True
    return False


def _scan_python(name: str, blob: bytes, direct: _Direct, db_defs: set[str], own: set[str]) -> None:
    try:
        tree = ast.parse(blob.decode("utf-8", "replace"))
    except SyntaxError:
        return
    aliases, internal = _aliases(tree, own)
    direct.hosts.update(_assigned_hosts(tree))
    local_funcs: set[str] = set()
    class_methods: dict[str, set[str]] = {}
    class_nodes: list[ast.ClassDef] = []
    func_calls: dict[str, set[str]] = {}
    func_strong: set[str] = set()
    func_deps: dict[str, set[str]] = {}
    func_constructed: dict[str, set[str]] = {}
    local_strong: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_funcs.add(node.name)
            direct.factories.add(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = _method_names(node)
            class_methods[node.name] = methods
            class_nodes.append(node)
            for method in methods:
                local_funcs.add(f"{node.name}.{method}")

    def walk_func(qual: str, node: ast.AST, class_name: str | None) -> None:
        calls: set[str] = set()
        deps: set[str] = set()
        strong = False
        constructed: set[str] = set()
        for child in _body_calls(node):
            target = _call_name(child.func)
            if not target:
                continue
            expanded = _expand(target, aliases)
            tail = expanded.rsplit(".", 1)[-1]
            head = expanded.split(".", 1)[0]
            if tail in _STRONG_TAILS and (
                expanded.startswith("http.client.")
                or expanded.startswith("socket.")
                or expanded.startswith("ssl.")
                or head in {"HTTPConnection", "HTTPSConnection"}
            ):
                strong = True
                direct.stdlib = _stdlib_of(expanded) or direct.stdlib
            if class_name and target.startswith("self."):
                head = target.split(".", 1)[1].split(".", 1)[0]
                if head in class_methods.get(class_name, ()):
                    calls.add(f"{class_name}.{head}")
            elif target in local_funcs:
                calls.add(target)
            root_name = target.split(".", 1)[0]
            if root_name in internal:
                continue
            bound = aliases.get(root_name, "")
            module = bound.rsplit(".", 1)[0] if "." in bound else bound
            dep_root = module.split(".", 1)[0] if module else ""
            if not bound or not module:
                continue
            if _is_our_module(module, own) or _is_our_module(bound, own):
                continue
            if dep_root in _STDLIB_IO or _is_stdlib_root(dep_root):
                if tail[:1].isupper():
                    local_strong.add(qual)
                continue
            deps.add(dep_root)
            if "." in module:
                deps.add(module.split(".", 1)[0] + "." + module.split(".")[1])
            if tail[:1].isupper():
                constructed.add(module.split(".")[1] if module.count(".") >= 1 else dep_root)
                constructed.add(dep_root)
        func_calls[qual] = calls
        func_deps[qual] = deps
        func_constructed[qual] = constructed
        if strong:
            func_strong.add(qual)
            local_strong.add(qual)
            direct.strong_tails.add(qual.split(".")[-1])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _DB_NAMES:
                db_defs.add(node.name)
            walk_func(node.name, node, None)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in _DB_NAMES:
                        db_defs.add(child.name)
                        direct.constructors.add(node.name)
                    walk_func(f"{node.name}.{child.name}", child, node.name)

    reaching = set(func_strong)
    changed = True
    while changed:
        changed = False
        for qual, calls in func_calls.items():
            if qual in reaching:
                continue
            if any(call in reaching for call in calls):
                reaching.add(qual)
                changed = True
    if func_strong:
        direct.strong = True
    for qual in reaching:
        direct.public_io.add(qual.split(".")[-1])
        direct.used_roots.update(func_deps.get(qual, ()))
        class_name = qual.split(".", 1)[0] if "." in qual else ""
        if class_name and _constructor_name(class_name):
            direct.constructors.add(class_name)
        elif "." not in qual and _constructor_name(qual):
            direct.constructors.add(qual)
    for qual, deps in func_deps.items():
        if not deps:
            continue
        direct.used_roots.update(deps)
        class_name = qual.split(".", 1)[0] if "." in qual else ""
        if class_name and _constructor_name(class_name):
            direct.constructors.add(class_name)
        elif "." not in qual and _constructor_name(qual):
            direct.constructors.add(qual)
    http_classes: set[str] = set()
    for qual in (*func_deps, *func_strong, *local_strong):
        if "." not in qual:
            if qual in _HTTP_SURFACE:
                direct.http_surface = True
            continue
        class_name, method = qual.split(".", 1)
        if method in _HTTP_SURFACE:
            http_classes.add(class_name)
            direct.http_surface = True
    for class_name, methods in class_methods.items():
        if not _product_class(class_name, class_methods, http_classes):
            continue
        roots: set[str] = set()
        strong_here = False
        seen: set[str] = set()
        pending = [f"{class_name}.{method}" for method in methods if _factory_method(method)]
        while pending:
            qual = pending.pop()
            if qual in seen:
                continue
            seen.add(qual)
            roots.update(func_constructed.get(qual, ()))
            if qual in local_strong or qual in func_strong:
                strong_here = True
            for call in func_calls.get(qual, ()):
                if call.startswith(class_name + "."):
                    pending.append(call)
        if roots:
            direct.domain_candidates.setdefault(class_name, set()).update(roots)
            direct.constructed_roots.update(roots)
        if strong_here:
            direct.domain_strong.add(class_name)
    if not _incidental_module(name):
        for node in class_nodes:
            class_name = node.name
            methods = class_methods[class_name]
            # A pool opens a connection, hands one out, and takes it back.
            if _lends_connections(node, reaching):
                direct.pool = True
            if "next" in methods and ({"__iter__", "__next__", "__anext__", "__aiter__"} & methods):
                direct.cursor = True
            if (
                _client_type(class_name)
                and _product_class(class_name, class_methods, http_classes)
                and not _pipeline_client(class_name, class_methods)
            ):
                direct.client_classes.add(class_name)
    if _uses_struct(tree, aliases):
        direct.wire = True
    _note_caller_hits(direct, class_methods, func_calls, func_constructed)
    _note_db_delegates(direct, func_calls, func_deps)


def _lends_connections(node: ast.ClassDef, reaching: set[str]) -> bool:
    """A class that opens a connection, hands one out, and takes it back.

    Handing one out takes an item off an instance collection. Taking it back
    puts an item on that same collection. Opening one is a method of this
    class whose calls reach a socket, including through a helper. Method
    names are not the signal.
    """
    taken: set[str] = set()
    returned: set[str] = set()
    for method in _iter_methods(node):
        for child in ast.walk(method):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                continue
            collection = _self_collection(child.func.value)
            if not collection:
                continue
            if child.func.attr in {"pop", "popleft"}:
                taken.add(collection)
            elif child.func.attr in {"append", "appendleft", "put"}:
                returned.add(collection)
    if not (taken & returned):
        return False
    prefix = f"{node.name}."
    return any(qual.startswith(prefix) for qual in reaching)


def _self_collection(node: ast.AST) -> str:
    """``self.name`` when that is the collection a call operates on."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return ""


def _iter_methods(node: ast.ClassDef) -> list[ast.AST]:
    found: list[ast.AST] = []

    def walk(body: list[ast.stmt]) -> None:
        for child in body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(child)
            elif isinstance(child, ast.If):
                walk(child.body)
                walk(child.orelse)
            elif isinstance(child, ast.Try):
                walk(child.body)
                walk(child.orelse)
                walk(child.finalbody)
                for handler in child.handlers:
                    walk(handler.body)

    walk(node.body)
    return found


def _method_names(node: ast.ClassDef) -> set[str]:
    """Method names, including ones defined under a class-body ``if``."""
    found: set[str] = set()

    def walk(body: list[ast.stmt]) -> None:
        for child in body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(child.name)
            elif isinstance(child, ast.If):
                walk(child.body)
                walk(child.orelse)
            elif isinstance(child, ast.Try):
                walk(child.body)
                walk(child.orelse)
                walk(child.finalbody)
                for handler in child.handlers:
                    walk(handler.body)

    walk(node.body)
    return found


def _client_type(name: str) -> bool:
    """A class the caller constructs. HTTP transports use the same words and are filtered apart."""
    if not name or name.startswith("_"):
        return False
    return name in {"Client", "Session"} or name.endswith(("Client", "Session"))


def _incidental_module(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    return any(part in _SKIP_PARTS or part.startswith("test_") or "pytest" in part for part in parts)


def _constructor_name(name: str) -> bool:
    """Class names and connection factories. Ordinary method names are not constructors."""
    if not name or name.startswith("_"):
        return False
    return name[:1].isupper() or name in {"client", "connect", "resource"}


def _factory_method(qual: str) -> bool:
    method = qual.split(".", 1)[-1]
    return method in {"__init__", "__new__", "create"} or method.startswith("from_")


def _pipeline_client(class_name: str, class_methods: dict[str, set[str]]) -> bool:
    """A client whose only public operations send an HTTP request.

    Product clients add their own operations on top of that. A core layer
    that only exposes the pipeline is a transport.
    """
    public = {
        method
        for method in class_methods.get(class_name, ())
        if not method.startswith("_") and method not in {"close", "aclose"}
    }
    return bool(public) and public <= (_HTTP_SURFACE | {"send_request", "open"})


def _uses_struct(tree: ast.AST, aliases: dict[str, str]) -> bool:
    """Binary framing. HTTP libraries do not pack message headers this way."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        expanded = _expand(_call_name(node.func), aliases)
        if expanded in {"struct.pack", "struct.unpack", "struct.pack_into", "struct.unpack_from"}:
            return True
    return False


def _note_caller_hits(
    direct: _Direct,
    class_methods: dict[str, set[str]],
    func_calls: dict[str, set[str]],
    func_constructed: dict[str, set[str]],
) -> None:
    """Count deps reached from public methods other than the constructor."""

    def reached(start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        roots: set[str] = set()
        while pending:
            qual = pending.pop()
            if qual in seen:
                continue
            seen.add(qual)
            roots.update(func_constructed.get(qual, ()))
            pending.extend(func_calls.get(qual, ()))
        return roots

    for class_name, methods in class_methods.items():
        for method in methods:
            if method.startswith("_") or method in {"__init__", "__new__"}:
                continue
            if method == "create" or method.startswith("create_"):
                continue
            for root in reached(f"{class_name}.{method}"):
                direct.caller_hits[root] = direct.caller_hits.get(root, 0) + 1


def _note_db_delegates(
    direct: _Direct,
    func_calls: dict[str, set[str]],
    func_deps: dict[str, set[str]],
) -> None:
    """Remember which external roots DB-API methods call.

    ``self`` calls stay inside the class. A method reached that way in one
    module counts for an override of the same method in another module: the
    base defines ``connect`` and the backend defines the helper ``connect``
    calls. The helper's name is the link, not a list of frameworks.
    """
    pending = [qual for qual in func_calls if qual.split(".")[-1] in _DB_NAMES]
    seen: set[str] = set()
    while pending:
        qual = pending.pop()
        if qual in seen:
            continue
        seen.add(qual)
        direct.db_reach.add(qual.split(".")[-1])
        pending.extend(func_calls.get(qual, ()))
    for qual, deps in func_deps.items():
        if not deps:
            continue
        tail = qual.split(".")[-1]
        direct.deps_by_tail.setdefault(tail, set()).update(deps)


def _product_class(class_name: str, class_methods: dict[str, set[str]], http_classes: set[str]) -> bool:
    if class_name.startswith("_") or class_name in http_classes:
        return False
    public = {method for method in class_methods.get(class_name, ()) if not method.startswith("_")}
    return bool(public) and not (public & _HTTP_SURFACE)


def _body_calls(node: ast.AST) -> list[ast.Call]:
    """Calls in the body and argument defaults, not in annotations."""
    found: list[ast.Call] = []
    body = getattr(node, "body", None)
    if isinstance(body, list):
        for stmt in body:
            for child in ast.walk(stmt):
                if isinstance(child, ast.Call):
                    found.append(child)
    args = getattr(node, "args", None)
    if isinstance(args, ast.arguments):
        for default in (*args.defaults, *args.kw_defaults):
            if default is None:
                continue
            for child in ast.walk(default):
                if isinstance(child, ast.Call):
                    found.append(child)
    return found


def _aliases(tree: ast.AST, own: set[str]) -> tuple[dict[str, str], set[str]]:
    found: dict[str, str] = {}
    internal: set[str] = set()

    def own_module(module: str) -> bool:
        return _is_our_module(module, own)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                found[local] = _STDLIB_IO.get(alias.name, alias.name)
                if own_module(alias.name):
                    internal.add(local)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                for alias in node.names:
                    internal.add(alias.asname or alias.name)
                continue
            if not node.module:
                continue
            base = _STDLIB_IO.get(node.module, node.module)
            inside = own_module(node.module)
            for alias in node.names:
                local = alias.asname or alias.name
                found[local] = f"{base}.{alias.name}"
                if inside:
                    internal.add(local)
    return found, internal


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _expand(target: str, aliases: dict[str, str]) -> str:
    head, _, tail = target.partition(".")
    bound = aliases.get(head)
    if not bound:
        return target
    return bound if not tail else f"{bound}.{tail}" if "." not in bound or tail else f"{bound}.{tail}"


def _stdlib_of(expanded: str) -> str:
    for name in ("http.client", "socket", "ssl", "urllib.request"):
        if expanded == name or expanded.startswith(name + "."):
            return name
    if expanded.split(".", 1)[0] in {"HTTPConnection", "HTTPSConnection"}:
        return "http.client"
    return ""


def _assigned_hosts(tree: ast.AST) -> set[str]:
    """Hosts written into an endpoint default, not a documentation link.

    The target name has to say that it is a URL, a host, an endpoint, or a
    server. A name one client coins for that default is not required.
    """
    hosts: set[str] = set()

    def take(node: ast.AST | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            host = _host_of(node.value)
            if host:
                hosts.add(host)
        elif isinstance(node, ast.IfExp):
            take(node.body)
            take(node.orelse)
        elif isinstance(node, ast.Call):
            # os.environ.get("BASE", "https://api.example.com/v1")
            for arg in node.args:
                take(arg)
            for keyword in node.keywords:
                if keyword.value is not None:
                    take(keyword.value)

    def named(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            text = target.id
        elif isinstance(target, ast.Attribute):
            text = target.attr
        else:
            return False
        lowered = text.lower().strip("_")
        # ``*_base`` is a URL default (``api_base``, ``upload_api_base``). The
        # specific name is not the signal.
        return "_base" in lowered or any(
            token in lowered for token in ("base_url", "api_host", "endpoint", "server_url", "host")
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(named(target) for target in node.targets):
            take(node.value)
        elif isinstance(node, ast.AnnAssign) and node.target is not None and named(node.target):
            take(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            defaults = [*args.defaults]
            positional = [arg.arg for arg in (*args.posonlyargs, *args.args)]
            for arg_name, default in zip(positional[-len(defaults) :], defaults):
                if named(ast.Name(id=arg_name)):
                    take(default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None and named(ast.Name(id=arg.arg)):
                    take(default)
    return hosts


def _is_stdlib_root(name: str) -> bool:
    root = (name or "").split(".", 1)[0]
    return bool(root) and root in frozenset(getattr(sys, "stdlib_module_names", ()))


def _host_of(text: str) -> str:
    match = re.search(r"https?://([^/\s\"']+)", text)
    if not match:
        return ""
    host = match.group(1).split("@")[-1].split(":")[0].lower().strip(".")
    labels = host.split(".")
    if len(labels) < 3:
        return ""
    if any(not re.fullmatch(r"[a-z0-9-]+", label) or not label for label in labels):
        return ""
    if labels[-1] in _RESERVED_TLDS:
        return ""
    if labels[0] in {"docs", "doc", "www", "en", "wiki"}:
        return ""
    return host


def _spec_kind(name: str, blob: bytes) -> str:
    lowered = name.lower()
    if lowered.endswith("service-2.json") or lowered.endswith("service-2.json.gz"):
        return "service-model"
    if not lowered.endswith((".json", ".yaml", ".yml")):
        return ""
    if "openapi" not in lowered and "swagger" not in lowered and "service-2" not in lowered:
        return ""
    head = blob[:4000].decode("utf-8", "replace").lower()
    if "openapi" in head or "swagger" in head:
        return "openapi"
    if "operations" in head and "shapes" in head:
        return "service-model"
    return ""


def _local_code(norm: str, version: str, root: Path | None) -> _Code | None:
    installed = _installed_code(norm, version, root)
    if installed is not None:
        return installed
    if not version:
        return None
    for directory, source in _wheel_dirs():
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            parsed = _dist_from_filename(path.name)
            if parsed is None:
                continue
            file_name, file_version = parsed
            if file_name != norm:
                continue
            if version and file_version != version:
                continue
            code = _read_archive(path, file_name, file_version, source=source)
            if code is not None:
                return code
    return None


def _installed_code(norm: str, version: str, root: Path | None) -> _Code | None:
    infos: list[Path] = []
    if root is not None:
        infos.extend(_dist_infos(root))
    try:
        from importlib.metadata import distribution
    except ImportError:
        distribution = None  # type: ignore[assignment]
    for info in infos:
        meta = _meta_map(info)
        name = normalize_dist(meta.get("name", ""))
        dist_version = meta.get("version", "")
        if name != norm:
            continue
        if version and dist_version and dist_version != version:
            continue
        files = _files_next_to(info)
        if not files:
            continue
        modules = _import_modules(files, _top_level_file(info), meta.get("name") or norm)
        return _Code(
            package=meta.get("name") or norm,
            version=dist_version,
            files=files,
            requires=_requires(meta.get("requires", "")),
            modules=modules,
            source="installed",
        )
    if distribution is None or root is not None:
        return None
    return None


def _files_next_to(info: Path) -> list[tuple[str, bytes]]:
    site = info.parent
    roots = _top_level_file(info)
    files: list[tuple[str, bytes]] = []
    for root_name in roots:
        module = site / f"{root_name}.py"
        if module.is_file():
            files.append((f"{root_name}.py", module.read_bytes()))
        folder = site / root_name
        if folder.is_dir():
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".py", ".c", ".h", ".json", ".yaml", ".yml"}:
                    continue
                if _skipped(path.relative_to(site).as_posix()):
                    continue
                files.append((path.relative_to(site).as_posix(), path.read_bytes()))
    return files


def _download_code(norm: str, version: str) -> _Code | None:
    if not version:
        version = _pypi_version(norm) or ""
    if not version:
        return None
    meta = _pypi_json(f"https://pypi.org/pypi/{norm}/{version}/json")
    if not meta:
        return None
    urls = meta.get("urls") if isinstance(meta, dict) else None
    if not isinstance(urls, list):
        return None
    chosen = _choose_url(urls)
    if chosen is None:
        return None
    filename = str(chosen.get("filename") or "")
    url = str(chosen.get("url") or "")
    if not filename or not url:
        return None
    dest = Path(tempfile.gettempdir()) / "conduit-wheels" / filename
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _fetch_to(url, dest)
        except Exception:
            return None
    info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
    package = str(info.get("name") or norm)
    return _read_archive(
        dest,
        normalize_dist(package),
        str(info.get("version") or version),
        requires_text=_requires_from_json(info),
        source="pypi",
    )


def _read_archive(
    path: Path,
    norm: str,
    version: str,
    requires_text: list[str] | None = None,
    source: str = "",
) -> _Code | None:
    files: list[tuple[str, bytes]] = []
    meta_text = ""
    top_level: tuple[str, ...] = ()
    try:
        if path.suffix == ".whl" or path.name.endswith(".whl"):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or _skipped(info.filename):
                        continue
                    lower = info.filename.lower()
                    if lower.endswith("/metadata") or lower.endswith(".dist-info/metadata"):
                        meta_text = archive.read(info).decode("utf-8", "replace")
                    if lower.endswith("top_level.txt"):
                        top_level = _lines(archive.read(info))
                    if lower.endswith("service-2.json.gz") or lower.endswith("/service-2.json"):
                        files.append((info.filename, b""))
                        continue
                    if not lower.endswith((".py", ".c", ".h", ".json", ".yaml", ".yml")):
                        continue
                    if info.file_size > 2_000_000 and not lower.endswith(("service-2.json", ".py")):
                        continue
                    files.append((info.filename, archive.read(info)))
        else:
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if not member.isfile() or _skipped(member.name):
                        continue
                    lower = member.name.lower()
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    blob = extracted.read()
                    if lower.endswith("pkg-info"):
                        meta_text = blob.decode("utf-8", "replace")
                    if not lower.endswith((".py", ".c", ".h", ".json", ".yaml", ".yml")):
                        continue
                    files.append((member.name.split("/", 1)[-1], blob))
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    if not files and not meta_text:
        return None
    meta = _meta_from_text(meta_text)
    requires = requires_text if requires_text is not None else _requires(meta.get("requires", ""))
    package = meta.get("name") or norm
    modules = _import_modules(files, top_level, package)
    return _Code(
        package=package,
        version=meta.get("version") or version,
        files=files,
        requires=requires,
        modules=modules,
        source=source,
    )


def _import_modules(files: list[tuple[str, bytes]], top_level: tuple[str, ...], package: str) -> tuple[str, ...]:
    """Import roots for a distribution.

    A wheel's top-level list wins. An sdist that ships its package under a
    build directory still imports as the distribution name.
    """
    modules = _modules_from(files, top_level)
    if top_level:
        return modules
    name = normalize_dist(package).replace("-", "_")
    if name.isidentifier():
        return (name,)
    return modules


def _modules_from(files: list[tuple[str, bytes]], top_level: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for name in top_level:
        if name.isidentifier() and name not in found:
            found.append(name)
    for path, _blob in files:
        pure = path.split("/", 1)[-1] if ".dist-info/" in path else path
        if not pure.endswith("/__init__.py") and not pure.endswith(".py"):
            continue
        if pure.endswith("/__init__.py"):
            dotted = pure[: -len("/__init__.py")].replace("/", ".")
        else:
            dotted = pure[:-3].replace("/", ".")
        parts = [part for part in dotted.split(".") if part]
        if not parts or not all(part.isidentifier() for part in parts):
            continue
        if parts[0] not in found:
            found.append(parts[0])
        if len(parts) <= 4 and ".".join(parts) not in found and pure.endswith("/__init__.py"):
            found.append(".".join(parts))
    return tuple(found)


def _requires(blob: str) -> list[str]:
    names: list[str] = []
    for line in blob.splitlines():
        if not line.lower().startswith("requires-dist:"):
            continue
        body = line.split(":", 1)[1].strip()
        dep, _, marker = body.partition(";")
        if "extra" in marker:
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", dep.strip())
        if match:
            norm = normalize_dist(match.group(1))
            if norm and norm not in names and norm != "python":
                names.append(norm)
    return names


def _requires_from_json(info: dict) -> list[str]:
    requires = info.get("requires_dist")
    if not isinstance(requires, list):
        return []
    return _requires("\n".join(f"Requires-Dist: {item}" for item in requires if isinstance(item, str)))


def _choose_url(urls: list) -> dict | None:
    wheels = []
    sdists = []
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    for item in urls:
        if not isinstance(item, dict):
            continue
        kind = item.get("packagetype")
        size = int(item.get("size") or 0)
        if size > _MAX_BYTES:
            continue
        if kind == "bdist_wheel":
            wheels.append(item)
        elif kind == "sdist":
            sdists.append(item)

    def usable(item: dict) -> bool:
        name = str(item.get("filename") or "")
        if "none-any" in name:
            return True
        return "manylinux" in name and "x86_64" in name and tag in name

    good = [item for item in wheels if usable(item)]
    if good:
        good.sort(key=lambda item: int(item.get("size") or 0))
        return good[0]
    if sdists:
        sdists.sort(key=lambda item: int(item.get("size") or 0))
        return sdists[0]
    return None


_PYPI_VERSIONS: dict[str, str] = {}


def _pypi_version(name: str) -> str:
    cached = _PYPI_VERSIONS.get(name)
    if cached is not None:
        return cached
    meta = _pypi_json(f"https://pypi.org/pypi/{name}/json")
    version = ""
    if meta:
        info = meta.get("info") if isinstance(meta, dict) else None
        if isinstance(info, dict):
            version = str(info.get("version") or "")
    _PYPI_VERSIONS[name] = version
    return version


def _pypi_json(url: str) -> dict | None:
    try:
        with urlopen(Request(url, headers={"User-Agent": "conduit-classify"}), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _fetch_to(url: str, dest: Path) -> None:
    with urlopen(Request(url, headers={"User-Agent": "conduit-classify"}), timeout=120) as response:
        blob = response.read(_MAX_BYTES + 1)
    if len(blob) > _MAX_BYTES:
        return
    dest.write_bytes(blob)


def _wheel_dirs() -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = [(Path(tempfile.gettempdir()) / "conduit-wheels", "cache")]
    fixture = Path(__file__).resolve().parents[3] / "fixtures" / "impact" / "wheels"
    if fixture.is_dir():
        dirs.append((fixture, "fixture"))
    return dirs


def _dist_from_filename(filename: str) -> tuple[str, str] | None:
    match = re.match(
        r"^(?P<name>[A-Za-z0-9_.-]+)-(?P<ver>\d[^/-]*?)(?:\.tar\.gz|-.*\.whl)$",
        filename,
    )
    if not match:
        return None
    return normalize_dist(match.group("name")), match.group("ver")


def _dist_infos(root: Path) -> list[Path]:
    found: list[Path] = []
    for folder in (".venv", "venv"):
        lib = root / folder / "lib"
        if not lib.is_dir():
            continue
        found.extend(lib.glob("python*/site-packages/*.dist-info"))
    return found


def _venv_import_index(root: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for info in _dist_infos(root):
        meta = _meta_map(info)
        name = normalize_dist(meta.get("name", ""))
        if not name:
            continue
        for imported in _top_level_file(info):
            index.setdefault(imported, name)
    return index


def _meta_map(info: Path) -> dict[str, str]:
    for filename in ("METADATA", "PKG-INFO"):
        path = info / filename
        if path.is_file():
            try:
                return _meta_from_text(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return {}
    return {}


def _meta_from_text(text: str) -> dict[str, str]:
    name = ""
    version = ""
    requires: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if lower.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif lower.startswith("version:"):
            version = line.split(":", 1)[1].strip()
        elif lower.startswith("requires-dist:"):
            requires.append(line)
    return {"name": name, "version": version, "requires": "\n".join(requires)}


def _top_level_file(info: Path) -> tuple[str, ...]:
    path = info / "top_level.txt"
    if not path.is_file():
        return ()
    try:
        return _lines(path.read_bytes())
    except OSError:
        return ()


def _lines(blob: bytes) -> tuple[str, ...]:
    names: list[str] = []
    for line in blob.decode("utf-8", "replace").splitlines():
        token = line.strip()
        if token.isidentifier() and token not in names:
            names.append(token)
    return tuple(names)


def _skipped(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    return any(part in _SKIP_PARTS or part.startswith("test_") for part in parts)


def _repo_skipped(path: Path) -> bool:
    """Skip vendor checkouts and caches. Do not skip a repo that lives under examples/."""
    from conduit.prune.grep_imports import SKIP_DIRS

    return any(part in SKIP_DIRS or part in {".cursor", "tests"} for part in path.parts)


def _python_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first = handle.readline(120)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    return b"python" in first.lower()


def _repo_sources(root: Path) -> list[Path]:
    """Python modules plus extensionless scripts that start with a Python shebang."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or _repo_skipped(path):
            continue
        if path.suffix == ".py" or (not path.suffix and _python_shebang(path)):
            found.append(path)
    return found


def _imported_modules(root: Path) -> set[str]:
    found: set[str] = set()
    for path in _repo_sources(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        found.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module)
                for alias in node.names:
                    # ``from parent.child import name`` is the child submodule.
                    if alias.name and alias.name.isidentifier() and alias.name[:1].islower():
                        found.add(f"{node.module}.{alias.name}")
    return found


def _is_local_module(root: Path, name: str) -> bool:
    if not name.isidentifier():
        return False
    bases = [root, root / "src", root / "app"]
    for base in bases:
        if (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file():
            return True
    return False


def _webhook_tokens(root: Path) -> list[str]:
    """Provider names from real string literals that mention webhooks or signatures.

    Source between quote characters is not a string. Scanning the file text for
    quote pairs can treat that span as one literal when an empty ``""`` shifts
    the matches, so words from comments and imports become fake distribution
    names. Only ``ast`` string constants count.
    """
    found: list[str] = []
    for path in _repo_sources(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            raw = node.value
            lowered = raw.lower()
            if "webhook" not in lowered and "signature" not in lowered:
                continue
            for part in re.split(r"[^A-Za-z0-9_.-]+", raw):
                token = part.strip("._-")
                if not _token_ok(token) or token in found:
                    continue
                found.append(token)
    return found


def _token_ok(token: str) -> bool:
    if len(token) < 3 or token.lower() in _STOP_TOKENS:
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", token))


def _cache_path(key: tuple[str, str]) -> Path:
    safe = re.sub(r"[^a-z0-9._-]+", "_", f"{key[0]}-{key[1]}")
    return cache_dir() / f"v{_SCHEMA}-{safe}.json"


def _cached(norm: str, version: str) -> Classification | None:
    key = (norm, version)
    hit = _MEMORY.get(key)
    if hit is not None:
        return hit
    disk = _read_disk(key)
    if disk is not None:
        _MEMORY[key] = disk
    return disk


def _read_disk(key: tuple[str, str]) -> Classification | None:
    from conduit.analysis_cache import cache_enabled

    if not cache_enabled():
        return None
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
        return None
    return Classification(
        name=str(payload.get("name") or key[0]),
        version=str(payload.get("version") or ""),
        package=str(payload.get("package") or ""),
        service=bool(payload.get("service")),
        kind=str(payload.get("kind") or ""),
        transport=bool(payload.get("transport")),
        constructors=tuple(payload.get("constructors") or ()),
        hosts=tuple(payload.get("hosts") or ()),
        host_mode=str(payload.get("host_mode") or "exact"),
        modules=tuple(payload.get("modules") or ()),
        evidence=str(payload.get("evidence") or ""),
        delegates=tuple(payload.get("delegates") or ()),
        loaded=True,
    )


def _write_disk(key: tuple[str, str], found: Classification) -> None:
    from conduit.analysis_cache import cache_enabled

    if not cache_enabled():
        return
    payload = {
        "schema": _SCHEMA,
        "name": found.name,
        "version": found.version,
        "package": found.package,
        "service": found.service,
        "kind": found.kind,
        "transport": found.transport,
        "constructors": list(found.constructors),
        "hosts": list(found.hosts),
        "host_mode": found.host_mode,
        "modules": list(found.modules),
        "evidence": found.evidence,
        "delegates": list(found.delegates),
    }
    try:
        _cache_path(key).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return


def _empty_buffer() -> io.BytesIO:
    return io.BytesIO()
