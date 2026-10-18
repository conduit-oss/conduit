"""Static Python call graph from an external service back to call sites.

Resolves imports (including aliases and package re-exports), instance
construction, and ``self`` method calls. Propagation walks callers of a
function that touches a service, recording each hop.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from conduit.outside_in.evidence import cached_classification
from conduit.outside_in.library_types import LibraryTypes, _typing_wrapper
from conduit.outside_in.specs import (
    HTTP_METHOD_TAILS,
    LIMITATIONS,
    ServiceSpec,
    evidence_spec,
    service_for_host,
    service_for_module,
)
from conduit.outside_in.model import Chain, Entry, Hop, ServiceMap
from conduit.prune.grep_imports import SKIP_DIRS

_MAX_HOPS = 12
_SKIP_PARTS = SKIP_DIRS | {".cursor", "tests"}
_CONF_RANK = {"high": 3, "medium": 2, "low": 1}
_CONF_NAME = {3: "high", 2: "medium", 1: "low"}


def min_confidence(left: str, right: str) -> str:
    return _CONF_NAME[min(_CONF_RANK.get(left, 1), _CONF_RANK.get(right, 1))]


@dataclass(frozen=True)
class Binding:
    kind: str
    target: str = ""
    qualname: str = ""
    confidence: str = "high"
    #: Cloud service from ``client("s3")`` / ``resource("s3")``, when it is a literal.
    cloud_service: str = ""
    #: ``base_url`` from an HTTP client constructor, when it is a string.
    base_url: str = ""
    #: Dotted path before a return type from another distribution was substituted.
    fallback: str = ""
    #: Other types that can reach this value, after the preferred one in ``target``.
    alternatives: tuple[str, ...] = ()


UNKNOWN = Binding("unknown")


@dataclass
class Def:
    qualname: str
    kind: str
    lineno: int
    params: tuple[str, ...]
    class_name: str | None
    node: ast.AST


@dataclass
class RawImport:
    lineno: int
    local: str
    level: int
    module: str
    imported: str | None


@dataclass
class CallRec:
    node: ast.Call
    enclosing: str
    lineno: int


@dataclass
class ModUnit:
    rel: str
    tree: ast.AST
    imports: list[RawImport] = field(default_factory=list)
    local_imports: dict[str, list[RawImport]] = field(default_factory=dict)
    defs: dict[str, Def] = field(default_factory=dict)
    calls: list[CallRec] = field(default_factory=list)
    assigns: dict[tuple[str, str], ast.expr] = field(default_factory=dict)
    #: Every assignment in source order: ``(scope, name) → [(end_line, end_col, expr)]``.
    #: A use sees the latest entry whose end is at or before the use.
    assign_sites: dict[tuple[str, str], list[tuple[int, int, ast.expr]]] = field(
        default_factory=dict
    )
    constants: dict[tuple[str, str], str] = field(default_factory=dict)
    annotations: dict[tuple[str, str], ast.expr] = field(default_factory=dict)
    returns: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: Yield expressions, used when the function is a context manager.
    yields: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: ``self.engine = factory(...)`` keyed by ``(class qualname, attr)``.
    #: Each write is ``(end_line, end_col, expr, scope)`` in source order.
    #: A read in that scope sees the latest write whose end is at or before the read.
    instance_assigns: dict[tuple[str, str], list[tuple[int, int, ast.expr, str]]] = field(
        default_factory=dict
    )
    #: Base expressions of each class, so a subclass can see fields assigned above it.
    class_bases: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: ``kwargs["temperature"] = 0.3`` keyed by ``(function, dict name)``.
    dict_keys: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: Same keys with the introducing line: ``(function, dict name) → [(key, lineno, expr)]``.
    dict_key_origins: dict[tuple[str, str], list[tuple[str, int, str]]] = field(
        default_factory=dict
    )


@dataclass
class _Direct:
    rel: str
    enclosing: str
    lineno: int
    expr: str
    service: str
    package: str
    sdk_method: str
    kind: str
    params: tuple[str, ...]
    response_fields: tuple[str, ...]
    returns_raw: bool
    confidence: str
    #: Splatted / partial keyword introductions: ``(name, lineno, expr)``.
    param_origins: tuple[tuple[str, int, str], ...] = ()


@dataclass
class _Edge:
    caller_rel: str
    caller: str
    callee_rel: str
    callee: str
    lineno: int
    expr: str
    keywords: tuple[str, ...]
    return_fields: tuple[str, ...]
    return_sites: tuple[tuple[int, str, str], ...]
    confidence: str


_SOURCE_ROOTS = {"src", "lib"}


class _ModuleIndex:
    def __init__(self, rels: list[str]) -> None:
        buckets: dict[str, set[str]] = {}
        for rel in rels:
            dotted = _dotted(rel)
            keys = [dotted]
            parts = [part for part in dotted.split(".") if part]
            # ``src/pkg/mod.py`` and ``app/src/pkg/mod.py`` are imported as
            # ``pkg.mod`` or ``src.pkg.mod``. A nested file whose last
            # component matches a dependency is not that dependency: only a
            # ``src`` / ``lib`` component opens the shorter name, and only
            # when that name is unique.
            for index, part in enumerate(parts):
                if part not in _SOURCE_ROOTS or index + 1 >= len(parts):
                    continue
                keys.append(".".join(parts[index:]))
                keys.append(".".join(parts[index + 1 :]))
            for key in keys:
                buckets.setdefault(key, set()).add(rel)
        self._exact = {
            key: next(iter(hits)) for key, hits in buckets.items() if len(hits) == 1
        }

    def lookup(self, dotted: str) -> str | None:
        return self._exact.get(dotted)


def analyze_python(root: Path, specs: tuple[ServiceSpec, ...]) -> ServiceMap:
    root = root.resolve()
    rels = _python_files(root)
    units: dict[str, ModUnit] = {}
    for rel in rels:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        units[rel] = _scan_module(rel, tree)
    index = _ModuleIndex(list(units))
    resolver = _Resolver(units, index, root=root)
    cloud_roots = _cloud_factory_roots(specs)
    resolver.cloud_roots = cloud_roots
    try:
        return _analyze_units(root, specs, units, resolver, cloud_roots)
    finally:
        resolver.close()


def _analyze_units(
    root: Path,
    specs: tuple[ServiceSpec, ...],
    units: dict[str, ModUnit],
    resolver: _Resolver,
    cloud_roots: frozenset[str],
) -> ServiceMap:
    entries: list[Entry] = []
    directs: list[_Direct] = []
    edges: list[_Edge] = []
    for rel, unit in units.items():
        entries.extend(resolver.import_entries(rel, specs))
        for call in unit.calls:
            resolved = resolver.resolve_call(unit, call)
            if resolved is None:
                # Generated clients call a helper with a verb and a path
                # before that helper is bound to an external SDK.
                http = _module_path_request(call, unit, specs)
                if http is not None:
                    spec, method, conf = http
                    directs.append(
                        _make_direct(
                            unit,
                            call,
                            spec=spec,
                            sdk_method=method,
                            kind="http_call",
                            confidence=conf,
                        )
                    )
                continue
            _binding, _confidence = resolved
            for binding in _call_bindings(_binding):
                confidence = binding.confidence
                http = _http_service(binding, call, unit, specs, root)
                if http is not None:
                    spec, method, conf = http
                    direct = _make_direct(
                        unit,
                        call,
                        spec=spec,
                        sdk_method=method,
                        kind="http_call",
                        confidence=min_confidence(confidence, conf),
                    )
                    extra = _http_payload_keys(call.node, unit, call.enclosing)
                    if extra:
                        direct.params = tuple(dict.fromkeys((*extra, *direct.params)))
                    directs.append(direct)
                    directs.extend(_response_directs(unit, call, direct))
                    continue
                spec = _service_of(binding, specs)
                if spec is None:
                    delegated = _delegated_binding(resolver, binding, specs)
                    if delegated is not None:
                        binding = delegated
                        spec = _service_of(binding, specs)
                        confidence = min_confidence(confidence, "medium")
                if binding.fallback:
                    fluent = Binding(
                        binding.kind,
                        binding.fallback,
                        confidence=binding.confidence,
                        cloud_service=binding.cloud_service,
                        base_url=binding.base_url,
                    )
                    fluent_spec = _service_of(fluent, specs)
                    if spec is None and fluent_spec is not None:
                        binding = fluent
                        spec = fluent_spec
                        confidence = binding.confidence
                if spec is None:
                    symbol = _symbol_target(binding, units)
                    if symbol is not None:
                        edges.append(
                            _make_edge(unit, call, symbol[0], symbol[1], confidence)
                        )
                    continue
                kind = _sdk_kind(spec, binding)
                method = _cloud_method(spec, binding, call, unit, cloud_roots)
                direct = _make_direct(
                    unit,
                    call,
                    spec=spec,
                    sdk_method=method,
                    kind=kind,
                    confidence=confidence,
                )
                directs.append(direct)
                directs.extend(_response_directs(unit, call, direct))
    from conduit.outside_in.webhooks import webhook_directs

    directs.extend(webhook_directs(units, specs))
    directs.extend(_exception_refs(units, resolver, specs))
    directs.extend(_version_gates(units, resolver, specs))
    chains = _propagate(directs, edges)
    entries.extend(_entries_from_directs(directs))
    entries = _dedupe_entries(entries)
    chains = _dedupe_chains(chains)
    evidence = {spec.service_id: spec.evidence for spec in specs if spec.evidence}
    for direct in directs:
        if direct.service not in evidence:
            matched = next((spec for spec in specs if spec.service_id == direct.service), None)
            if matched is not None and matched.evidence:
                evidence[direct.service] = matched.evidence
    return ServiceMap(
        root=str(root),
        entries=tuple(entries),
        chains=tuple(chains),
        limitations=LIMITATIONS,
        evidence=evidence,
    )


def _python_files(root: Path) -> list[str]:
    rels: list[str] = []
    seen: set[str] = set()
    for path in root.rglob("*.py"):
        rel = _rel_python_file(root, path)
        if rel and rel not in seen:
            seen.add(rel)
            rels.append(rel)
    # Extensionless scripts with a Python shebang (e.g. git-github-login).
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix:
            continue
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        if not _has_python_shebang(path):
            continue
        rel = _rel_python_file(root, path)
        if rel and rel not in seen:
            seen.add(rel)
            rels.append(rel)
    rels.sort()
    return rels


def _rel_python_file(root: Path, path: Path) -> str | None:
    if not path.is_file():
        return None
    if any(part in _SKIP_PARTS for part in path.parts):
        return None
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _has_python_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            first = handle.readline(120)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    text = first.decode("utf-8", errors="ignore").lower()
    return "python" in text


def _dotted(rel: str) -> str:
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", ".")
    if rel.endswith(".py"):
        return rel[:-3].replace("/", ".")
    return rel.replace("/", ".")


def _package_of(rel: str) -> str:
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", ".")
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0].replace("/", ".")


def _scan_module(rel: str, tree: ast.AST) -> ModUnit:
    unit = ModUnit(rel=rel, tree=tree)
    _Scanner(unit).visit(tree)
    return unit


class _Scanner(ast.NodeVisitor):
    def __init__(self, unit: ModUnit) -> None:
        self.unit = unit
        self.enclosing = "<module>"
        self.class_name: str | None = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = alias.name
            local = alias.asname or module.split(".")[0]
            bound = module if alias.asname else module.split(".")[0]
            self._add_import(
                RawImport(node.lineno, local, 0, bound, None)
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self._add_import(
                    RawImport(node.lineno, "*", node.level, module, "*")
                )
                continue
            local = alias.asname or alias.name
            self._add_import(
                RawImport(node.lineno, local, node.level, module, alias.name)
            )

    def _add_import(self, raw: RawImport) -> None:
        if self.enclosing == "<module>":
            self.unit.imports.append(raw)
        else:
            self.unit.local_imports.setdefault(self.enclosing, []).append(raw)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qual(node.name)
        self.unit.defs[qual] = Def(
            qualname=qual,
            kind="class",
            lineno=node.lineno,
            params=(),
            class_name=self.class_name,
            node=node,
        )
        self.unit.class_bases[qual] = list(node.bases)
        previous, previous_class = self.enclosing, self.class_name
        self.enclosing = qual
        self.class_name = qual
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing = previous
        self.class_name = previous_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node, async_def=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node, async_def=True)

    def _function(self, node: ast.AST, *, async_def: bool) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        qual = self._qual(node.name)
        parent = self.unit.defs.get(self.enclosing)
        kind = "function"
        class_name = self.class_name
        if parent is not None and parent.kind == "class":
            kind = "async_method" if async_def else "method"
        elif async_def:
            kind = "async_function"
        self.unit.defs[qual] = Def(
            qualname=qual,
            kind=kind,
            lineno=node.lineno,
            params=_param_names(node),
            class_name=class_name,
            node=node,
        )
        previous = self.enclosing
        self.enclosing = qual
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if arg.annotation is not None:
                self.unit.annotations[(qual, arg.arg)] = arg.annotation
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing = previous

    def visit_Assign(self, node: ast.Assign) -> None:
        lineno = getattr(node, "lineno", 0) or 0
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Lambda):
                self._bind_lambda(name, node.value)
                return
            self._note_assign(name, node.value, node.value)
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self.unit.constants[(self.enclosing, name)] = node.value.value
            self._record_dict_value(name, node.value, lineno)
        for target in node.targets:
            self._record_instance_assign(target, node.value)
            self._record_dict_key(target, lineno)
        self.generic_visit(node)

    def _record_instance_assign(self, target: ast.AST, value: ast.expr) -> None:
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and self.class_name
        ):
            end_line, end_col = _end_pos(value)
            self.unit.instance_assigns.setdefault((self.class_name, target.attr), []).append(
                (end_line, end_col, value, self.enclosing)
            )

    def _record_dict_key(self, target: ast.AST, lineno: int) -> None:
        if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
            return
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = target.value.id
            expr = f"{name}[{key.value!r}]"
            self._add_dict_key_origin(self.enclosing, name, key.value, lineno, expr)

    def _record_dict_value(self, name: str, value: ast.expr, lineno: int) -> None:
        for key, line, expr in _dict_introductions(value, name, lineno):
            self._add_dict_key_origin(self.enclosing, name, key, line, expr)

    def _add_dict_key_origin(
        self, enclosing: str, dict_name: str, key: str, lineno: int, expr: str
    ) -> None:
        self.unit.dict_keys.setdefault((enclosing, dict_name), []).append(key)
        self.unit.dict_key_origins.setdefault((enclosing, dict_name), []).append(
            (key, lineno, expr)
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        lineno = getattr(node, "lineno", 0) or 0
        if isinstance(node.target, ast.Name):
            if node.annotation is not None:
                self.unit.annotations[(self.enclosing, node.target.id)] = node.annotation
            if isinstance(node.value, ast.Lambda):
                self._bind_lambda(node.target.id, node.value)
                return
            if node.value is not None:
                self._note_assign(node.target.id, node.value, node.value)
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    self.unit.constants[(self.enclosing, node.target.id)] = node.value.value
                self._record_dict_value(node.target.id, node.value, lineno)
        if (
            isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id in {"self", "cls"}
            and self.class_name
            and node.annotation is not None
        ):
            self.unit.annotations[(self.class_name, node.target.attr)] = node.annotation
        if node.value is not None:
            self._record_instance_assign(node.target, node.value)
            self._record_dict_key(node.target, lineno)
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        if node.value is not None and self.enclosing != "<module>":
            self.unit.yields.setdefault(self.enclosing, []).append(node.value)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._bind_with(node)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._bind_with(node)
        self.generic_visit(node)

    def _bind_with(self, node: ast.AST) -> None:
        items = getattr(node, "items", ())
        for item in items:
            if isinstance(item.optional_vars, ast.Name) and item.context_expr is not None:
                self._note_assign(item.optional_vars.id, item.context_expr, item.optional_vars)

    def _note_assign(self, name: str, value: ast.expr, bound: ast.AST) -> None:
        """Record ``name = value``. ``bound`` is the expression that ends where the name is visible."""
        self.unit.assigns[(self.enclosing, name)] = value
        end_line, end_col = _end_pos(bound)
        self.unit.assign_sites.setdefault((self.enclosing, name), []).append(
            (end_line, end_col, value)
        )

    def _bind_lambda(self, name: str, node: ast.Lambda) -> None:
        qual = self._qual(name)
        self.unit.defs[qual] = Def(
            qualname=qual,
            kind="function",
            lineno=getattr(node, "lineno", 0) or 0,
            params=_lambda_params(node),
            class_name=self.class_name,
            node=node,
        )
        previous = self.enclosing
        self.enclosing = qual
        self.visit(node.body)
        self.enclosing = previous

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.unit.returns.setdefault(self.enclosing, []).append(node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.unit.calls.append(CallRec(node, self.enclosing, _call_lineno(node)))
        self._record_dict_mutation(node)
        self.generic_visit(node)

    def _record_dict_mutation(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            return
        dict_name = node.func.value.id
        attr = node.func.attr
        lineno = getattr(node, "lineno", 0) or 0
        if attr == "update":
            for key, line, expr in _update_introductions(
                node, dict_name, lineno, self.unit, self.enclosing
            ):
                self._add_dict_key_origin(self.enclosing, dict_name, key, line, expr)
            return
        if attr == "setdefault" and node.args:
            key_node = node.args[0]
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                expr = f"{dict_name}.setdefault({key_node.value!r}, ...)"
                self._add_dict_key_origin(
                    self.enclosing, dict_name, key_node.value, lineno, expr
                )

    def _qual(self, name: str) -> str:
        if self.enclosing == "<module>":
            return name
        return f"{self.enclosing}.{name}"


def _param_names(node: ast.AST) -> tuple[str, ...]:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    names = [arg.arg for arg in node.args.args if arg.arg not in {"self", "cls"}]
    names.extend(arg.arg for arg in node.args.kwonlyargs)
    return tuple(names)


def _lambda_params(node: ast.Lambda) -> tuple[str, ...]:
    names = [arg.arg for arg in node.args.args]
    names.extend(arg.arg for arg in node.args.kwonlyargs)
    return tuple(names)


_RESOLVE_DEPTH = 32


class _Resolver:
    def __init__(
        self,
        units: dict[str, ModUnit],
        index: _ModuleIndex,
        root: Path | None = None,
    ) -> None:
        self.units = units
        self.index = index
        self.root = root
        self._bindings: dict[str, dict[str, Binding]] = {}
        self._binding_stack: set[str] = set()
        self._returns: dict[str, Binding] = {}
        self._param_types: dict[tuple[str, str, str], Binding] = {}
        self._libraries: LibraryTypes | None = None
        self._stem_index: dict[str, tuple[str, ...]] | None = None
        self._expr_memo: dict[tuple[str, str, int], Binding] = {}
        self._allow_fluent = True
        self.cloud_roots: frozenset[str] = frozenset()
        self._infer_returns()

    def close(self) -> None:
        """Release any analysis resources. Safe to call more than once."""

    def import_entries(self, rel: str, specs: tuple[ServiceSpec, ...]) -> list[Entry]:
        unit = self.units[rel]
        bindings = self._bindings_for(rel)
        out: list[Entry] = []
        for raw in unit.imports:
            if raw.imported == "*":
                spec = service_for_module(raw.module, specs) if raw.module else None
                if spec is not None and not spec.transport:
                    out.append(
                        Entry(
                            service=spec.service_id,
                            package=spec.package,
                            kind=f"{_kind_prefix(spec)}_import",
                            rel=rel,
                            lineno=raw.lineno,
                            symbol=f"from {raw.module} import *",
                            confidence="low",
                            detail="star-import is not bound to call sites",
                        )
                    )
                continue
            binding = bindings.get(raw.local)
            if binding is None:
                continue
            spec = _service_of(binding, specs)
            if spec is None:
                continue
            base = _kind_prefix(spec)
            kind = f"{base}_import" if raw.imported is None else f"{base}_alias"
            symbol = binding.target or raw.local
            detail = raw.local if raw.local == symbol else f"{raw.local} = {symbol}"
            out.append(
                Entry(
                    service=spec.service_id,
                    package=spec.package,
                    kind=kind,
                    rel=rel,
                    lineno=raw.lineno,
                    symbol=symbol,
                    confidence="high",
                    detail=detail,
                )
            )
        return out

    def resolve_call(
        self, unit: ModUnit, call: CallRec
    ) -> tuple[Binding, str] | None:
        binding = self._resolve_expr(call.node.func, unit, call.enclosing, set())
        if binding.kind == "unknown":
            return None
        return binding, binding.confidence

    def _library(self) -> LibraryTypes:
        if self._libraries is None:
            self._libraries = LibraryTypes(self.root)
        return self._libraries

    def _infer_returns(self) -> None:
        # Fluent reassignment is for call-site chains; during inference it
        # rewalks every ``x = x.m()`` hop across the repo and dominates runtime.
        previous = self._allow_fluent
        self._allow_fluent = False
        try:
            for _ in range(8):
                changed = self._infer_return_pass() or self._infer_parameter_pass()
                if not changed:
                    break
        finally:
            self._allow_fluent = previous

    def _infer_return_pass(self) -> bool:
        changed = False
        for unit in self.units.values():
            for qual, defn in unit.defs.items():
                if defn.kind == "class":
                    continue
                key = _sym(unit.rel, qual)
                if key in self._returns:
                    continue
                exprs = unit.yields.get(qual, []) if _is_context_manager(defn) else []
                exprs = exprs or unit.returns.get(qual, [])
                for expr in exprs:
                    binding = self._resolve_expr(expr, unit, qual, set())
                    if binding.kind in {"instance_external", "instance_local"}:
                        self._returns[key] = binding
                        changed = True
                        break
                if key in self._returns or defn.node is None:
                    continue
                announced = self._resolve_annotation(
                    getattr(defn.node, "returns", None), unit, qual, set()
                )
                announced = _as_instance(announced)
                if announced.kind in {"instance_external", "instance_local"}:
                    self._returns[key] = announced
                    changed = True
        return changed

    def _infer_parameter_pass(self) -> bool:
        """Give an unannotated parameter the type passed in at its call sites."""
        changed = False
        for unit in self.units.values():
            for call in unit.calls:
                func = self._resolve_expr(call.node.func, unit, call.enclosing, set())
                if func.kind != "symbol":
                    continue
                owner = self.units.get(func.target)
                if owner is None:
                    continue
                defn = owner.defs.get(func.qualname)
                if defn is None or not isinstance(defn.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = [
                    arg.arg
                    for arg in (
                        *defn.node.args.posonlyargs,
                        *defn.node.args.args,
                        *defn.node.args.kwonlyargs,
                    )
                    if arg.arg not in {"self", "cls"}
                ]
                positional = [
                    arg.arg
                    for arg in (*defn.node.args.posonlyargs, *defn.node.args.args)
                    if arg.arg not in {"self", "cls"}
                ]
                for name, expr in zip(positional, call.node.args):
                    if self._bind_param(owner, func.qualname, name, expr, unit, call.enclosing):
                        changed = True
                for keyword in call.node.keywords:
                    if keyword.arg and keyword.arg in params:
                        if self._bind_param(
                            owner, func.qualname, keyword.arg, keyword.value, unit, call.enclosing
                        ):
                            changed = True
        return changed

    def _bind_param(
        self,
        owner: ModUnit,
        qual: str,
        name: str,
        expr: ast.AST,
        caller: ModUnit,
        enclosing: str,
    ) -> bool:
        if (qual, name) in owner.annotations:
            return False
        key = (owner.rel, qual, name)
        if key in self._param_types:
            return False
        resolved = self._resolve_expr(expr, caller, enclosing, set())
        instanced = _as_instance(resolved)
        if instanced.kind == "instance_external" and instanced.target:
            self._param_types[key] = instanced
            return True
        # A class argument is an instance of that class for attribute reads
        # such as ``model.query`` on an ORM model passed into a helper.
        if (
            instanced.kind == "instance_local"
            and instanced.target
            and instanced.qualname
        ):
            self._param_types[key] = instanced
            return True
        return False

    def _bindings_for(self, rel: str) -> dict[str, Binding]:
        cached = self._bindings.get(rel)
        if cached is not None and rel not in self._binding_stack:
            return cached
        if rel in self._binding_stack:
            return self._bindings.get(rel, {})
        unit = self.units[rel]
        self._binding_stack.add(rel)
        result: dict[str, Binding] = {}
        self._bindings[rel] = result
        events: list[tuple[int, int, str, object]] = []
        for index, raw in enumerate(unit.imports):
            events.append((raw.lineno, index, "import", raw))
        for index, (qual, defn) in enumerate(unit.defs.items()):
            if "." in qual:
                continue
            events.append((defn.lineno, index, "def", defn))
        events.sort(key=lambda item: (item[0], item[1]))
        for _lineno, _index, kind, payload in events:
            if kind == "def":
                defn = payload
                assert isinstance(defn, Def)
                result[defn.qualname] = Binding("symbol", rel, defn.qualname)
            else:
                raw = payload
                assert isinstance(raw, RawImport)
                if raw.local == "*":
                    self._bind_star(raw, rel, result)
                    continue
                result[raw.local] = self._resolve_import(raw, rel)
        self._binding_stack.discard(rel)
        self._bindings[rel] = result
        return result

    def _lookup_module(self, rel: str, dotted: str) -> str | None:
        """Project file for an import.

        A file may be executed with a parent directory on the path, so
        ``methods.api`` from ``service/methods/work.py`` is
        ``service/methods/api.py``.
        """
        found = self.index.lookup(dotted)
        if found or dotted.count(".") < 1:
            return found
        parts = Path(rel).parent.parts
        extra = tuple(part for part in dotted.split(".") if part)
        for index in range(len(parts), 0, -1):
            found = self.index.lookup(".".join((*parts[:index], *extra)))
            if found:
                return found
        return None

    def _bind_star(self, raw: RawImport, rel: str, result: dict[str, Binding]) -> None:
        """Copy the public names of a project module into ``result``."""
        dotted = _absolute_module(rel, raw.level, raw.module)
        if not dotted:
            return
        found = self._lookup_module(rel, dotted)
        if not found or found == rel:
            return
        binds = self._bindings_for(found)
        for name in _public_names(self.units[found], binds):
            result[name] = binds[name]

    def _resolve_import(self, raw: RawImport, rel: str) -> Binding:
        dotted = _absolute_module(rel, raw.level, raw.module)
        if raw.imported is None:
            found = self._lookup_module(rel, dotted) if dotted else None
            if found:
                return Binding("module_local", found)
            if dotted:
                return Binding("external_module", dotted)
            return UNKNOWN
        if raw.imported == "*":
            return UNKNOWN
        if not dotted:
            return UNKNOWN
        found = self._lookup_module(rel, dotted)
        if found is None:
            return Binding("external_attr", f"{dotted}.{raw.imported}")
        binds = self._bindings_for(found)
        if raw.imported in binds:
            return binds[raw.imported]
        owner = self.units.get(found)
        if owner is not None:
            assigned = _visible_assign(owner, "<module>", raw.imported, None)
            if assigned is not None and not _is_none(assigned):
                return self._resolve_expr(assigned, owner, "<module>", set())
        parent = _dotted(found)
        sub = self.index.lookup(f"{parent}.{raw.imported}") if parent else None
        if sub:
            return Binding("module_local", sub)
        return UNKNOWN

    def _resolve_expr(
        self,
        node: ast.AST | None,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
    ) -> Binding:
        if node is None or depth > _RESOLVE_DEPTH:
            return UNKNOWN
        # Memoize only after inference. Inference runs before cloud factory
        # roots are set, and caching those Call results drops the service name
        # on ``session.client("iam")``.
        memo_key = (unit.rel, enclosing, id(node))
        if self._allow_fluent:
            cached = self._expr_memo.get(memo_key)
            if cached is not None:
                return cached
        marker = ("node", unit.rel, str(id(node)))
        if marker in stack:
            return UNKNOWN
        stack.add(marker)
        try:
            found = self._resolve_expr_node(node, unit, enclosing, stack, depth)
        finally:
            stack.discard(marker)
        if self._allow_fluent and found.kind != "unknown":
            self._expr_memo[memo_key] = found
        return found

    def _resolve_expr_node(
        self,
        node: ast.AST,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> Binding:
        nxt = depth + 1
        if isinstance(node, ast.Name):
            lineno = getattr(node, "lineno", 0) or 0
            use = (lineno, getattr(node, "col_offset", 0) or 0) if lineno else None
            return self._resolve_name(node.id, unit, enclosing, stack, nxt, use)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and self._is_import_local(
                unit, enclosing, node.value.id
            ):
                configured = self._config_module_attr(
                    node.value.id, node.attr, unit, enclosing, stack, nxt
                )
                if configured is not None:
                    return configured
            base = self._resolve_expr(node.value, unit, enclosing, stack, nxt)
            lineno = getattr(node, "lineno", 0) or 0
            use = (lineno, getattr(node, "col_offset", 0) or 0) if lineno else None
            return self._attribute(base, node.attr, stack, nxt, enclosing=enclosing, use=use)
        if isinstance(node, ast.Call):
            super_inst = self._super_instance(node, unit, enclosing, stack, nxt)
            if super_inst is not None:
                return super_inst
            aliased = self._alias_call(node, unit, enclosing, stack, nxt)
            if aliased is not None:
                return aliased
            func = self._resolve_expr(node.func, unit, enclosing, stack, nxt)
            cloud = _factory_service(func, node, unit, enclosing, self.cloud_roots) or func.cloud_service
            result = self._call_result(func, node, unit, enclosing, cloud_service=cloud)
            if result.kind == "unknown":
                default = self._mapping_get_default(node, unit, enclosing, stack, nxt)
                if default is not None:
                    return default
            return result
        if isinstance(node, ast.Subscript):
            base = self._resolve_expr(node.value, unit, enclosing, stack, nxt)
            if base.kind in {"instance_external", "external_attr"} and base.target:
                found = self._subscript_binding(base)
                if found is not None:
                    return found
            return UNKNOWN
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value:
            return self._resolve_dotted_text(node.value, unit, enclosing, stack, nxt)
        if isinstance(node, ast.BinOp):
            left = self._resolve_expr(node.left, unit, enclosing, stack, nxt)
            if left.kind != "unknown":
                return left
            return self._resolve_expr(node.right, unit, enclosing, stack, nxt)
        return UNKNOWN

    def _resolve_annotation(
        self,
        node: ast.AST | None,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]] | None = None,
        depth: int = 0,
    ) -> Binding:
        if node is None or depth > _RESOLVE_DEPTH:
            return UNKNOWN
        if stack is None:
            stack = set()
        nxt = depth + 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self._resolve_dotted_text(node.value, unit, enclosing, stack, nxt)
        if isinstance(node, ast.Subscript):
            if _typing_wrapper(node.value):
                return self._resolve_annotation(node.slice, unit, enclosing, stack, nxt)
            found = self._resolve_annotation(node.value, unit, enclosing, stack, nxt)
            if _as_instance(found).kind != "unknown":
                return found
            return self._resolve_annotation(node.slice, unit, enclosing, stack, nxt)
        if isinstance(node, ast.BinOp):
            left = self._resolve_annotation(node.left, unit, enclosing, stack, nxt)
            if _as_instance(left).kind != "unknown":
                return left
            return self._resolve_annotation(node.right, unit, enclosing, stack, nxt)
        if isinstance(node, ast.Tuple):
            for elt in node.elts:
                found = self._resolve_annotation(elt, unit, enclosing, stack, nxt)
                if _as_instance(found).kind != "unknown":
                    return found
            return UNKNOWN
        return self._resolve_expr(node, unit, enclosing, stack, nxt)

    def _resolve_dotted_text(
        self,
        text: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
    ) -> Binding:
        parts = [part for part in text.split(".") if part]
        if not parts or depth > _RESOLVE_DEPTH:
            return UNKNOWN
        binding = self._resolve_name(parts[0], unit, enclosing, stack, depth + 1)
        for attr in parts[1:]:
            if binding.kind == "unknown":
                return UNKNOWN
            binding = self._attribute(binding, attr, stack, depth + 1)
        return binding

    def _mapping_get_default(
        self,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
    ) -> Binding | None:
        """``kwargs.get(key, Factory())`` may be the default value.

        Only when the receiver is not itself an SDK object, so ``client.get``
        stays a call.
        """
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "get" or len(node.args) < 2:
            return None
        receiver = self._resolve_expr(node.func.value, unit, enclosing, stack, depth)
        if receiver.kind in {"external_attr", "external_module", "instance_external"}:
            return None
        default = self._resolve_expr(node.args[1], unit, enclosing, stack, depth)
        if default.kind == "unknown":
            return None
        return default

    def _fluent_reassign(
        self,
        assigned: ast.expr,
        name: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> Binding | None:
        """``name = name.m1().m2()`` keeps the receiver type across reassigns.

        Walking the whole right-hand side as a nested expression spends a hop
        per earlier reassignment, so a long ``query = query.filter(...)`` chain
        hits the depth limit before the last call. Peeling the fluent methods
        and resolving the prior ``name`` once keeps depth flat. Each method's
        result is whatever the library returns for that call.
        """
        if not self._allow_fluent:
            return None
        peeled = _fluent_call_steps(assigned, name)
        if peeled is None:
            return None
        receiver, steps = peeled
        use = (
            getattr(receiver, "lineno", 0) or 0,
            getattr(receiver, "col_offset", 0) or 0,
        )
        if not use[0]:
            return None
        base = self._resolve_name(name, unit, enclosing, stack, depth, use)
        if base.kind == "unknown":
            return None
        for call in steps:
            assert isinstance(call.func, ast.Attribute)
            lineno = getattr(call.func, "lineno", 0) or 0
            attr_use = (
                (lineno, getattr(call.func, "col_offset", 0) or 0) if lineno else None
            )
            func = self._attribute(
                base,
                call.func.attr,
                stack,
                depth,
                enclosing=enclosing,
                use=attr_use,
            )
            if func.kind == "unknown":
                return None
            base = self._call_result(func, call, unit, enclosing)
            if base.kind == "unknown":
                return None
        return base

    def _super_instance(
        self,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> Binding | None:
        """Zero-argument ``super()`` is an instance of the next base class.

        A name bound in this scope is that binding, not the builtin. The first
        base that resolves to a class is the instance. ``super(Explicit, obj)``
        starts after ``Explicit`` when that class is one of the bases.
        """
        if not isinstance(node.func, ast.Name) or node.func.id != "super":
            return None
        bound = self._resolve_name("super", unit, enclosing, stack, depth + 1)
        if bound.kind != "unknown":
            return None
        defn = unit.defs.get(enclosing)
        class_name = defn.class_name if defn is not None else None
        if not class_name:
            return None
        bases = self._class_symbols(unit, class_name, stack, depth)
        if not bases:
            return None
        start = 0
        if node.args:
            explicit = self._resolve_expr(node.args[0], unit, enclosing, stack, depth + 1)
            if explicit.kind == "symbol" and explicit.qualname and explicit.qualname != class_name:
                for index, parent in enumerate(bases):
                    if parent.target == explicit.target and parent.qualname == explicit.qualname:
                        start = index + 1
                        break
                else:
                    return None
        if start >= len(bases):
            return None
        parent = bases[start]
        return Binding(
            "instance_local",
            parent.target,
            parent.qualname,
            confidence=min_confidence(parent.confidence, "medium"),
        )

    def _class_symbols(
        self,
        unit: ModUnit,
        class_name: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> list[Binding]:
        found: list[Binding] = []
        for base_expr in unit.class_bases.get(class_name, []):
            while isinstance(base_expr, ast.Subscript):
                base_expr = base_expr.value
            parent = self._resolve_expr(base_expr, unit, class_name, stack, depth + 1)
            if parent.kind == "symbol" and parent.qualname:
                found.append(parent)
        return found

    def _alias_call(
        self,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
    ) -> Binding | None:
        """``functools.partial`` and literal ``getattr`` keep the underlying callable."""
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if not func_name or not node.args:
            return None
        func_binding = self._resolve_expr(node.func, unit, enclosing, stack, depth)
        if func_binding.kind == "symbol":
            return None
        if func_name == "partial" and _is_partial(func_binding):
            return self._resolve_expr(node.args[0], unit, enclosing, stack, depth)
        if func_name == "getattr" and len(node.args) >= 2:
            literal = _const_str(node.args[1], unit, enclosing)
            if not literal or not literal.isidentifier():
                return None
            obj = self._resolve_expr(node.args[0], unit, enclosing, stack, depth)
            if obj.kind == "unknown":
                return None
            return self._attribute(obj, literal, stack, depth)
        return None

    def _resolve_name(
        self,
        name: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
        use: tuple[int, int] | None = None,
    ) -> Binding:
        # Position is part of the token so ``name = name.method()`` can still
        # see the earlier value while the later use is being resolved.
        token = (
            "name",
            unit.rel,
            enclosing,
            name,
            "" if use is None else f"{use[0]}:{use[1]}",
        )
        if token in stack:
            return UNKNOWN
        stack.add(token)
        try:
            defn = unit.defs.get(enclosing)
            if name in {"self", "cls"} and defn is not None and defn.class_name:
                return Binding("instance_local", unit.rel, defn.class_name)
            assigned = _visible_assign(unit, enclosing, name, use)
            if assigned is not None and not _is_none(assigned):
                fluent = self._fluent_reassign(
                    assigned, name, unit, enclosing, stack, depth + 1
                )
                if fluent is not None:
                    return fluent
                return self._resolve_expr(assigned, unit, enclosing, stack, depth + 1)
            if name in _params_of(unit, enclosing):
                typed = self._annotated_binding(unit, enclosing, name, stack, depth)
                if typed is not None:
                    return typed
                inferred = self._param_types.get((unit.rel, enclosing, name))
                if inferred is not None:
                    return inferred
            if name not in _params_of(unit, enclosing):
                for scope in _enclosing_chain(enclosing):
                    for raw in unit.local_imports.get(scope, []):
                        if raw.local == name:
                            return self._resolve_import(raw, unit.rel)
            local_def = _local_def(unit, enclosing, name)
            if local_def is not None:
                return Binding("symbol", unit.rel, local_def)
            if enclosing != "<module>" and name not in _params_of(unit, enclosing):
                module_assign = _visible_assign(unit, "<module>", name, None)
                if module_assign is not None and not _is_none(module_assign):
                    return self._resolve_expr(module_assign, unit, "<module>", stack, depth + 1)
            binding = self._bindings_for(unit.rel).get(name)
            if binding is not None:
                return binding
            typed = self._annotated_binding(unit, enclosing, name, stack, depth)
            if typed is not None:
                return typed
            return UNKNOWN
        finally:
            stack.discard(token)

    def _annotated_binding(
        self,
        unit: ModUnit,
        enclosing: str,
        name: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> Binding | None:
        annotation = unit.annotations.get((enclosing, name)) or unit.annotations.get(
            ("<module>", name)
        )
        if annotation is None:
            return None
        typed = self._resolve_annotation(annotation, unit, enclosing, stack, depth + 1)
        instanced = _as_instance(typed)
        if instanced.kind == "unknown":
            return None
        return Binding(
            instanced.kind,
            instanced.target,
            instanced.qualname,
            confidence=min_confidence(instanced.confidence, "medium"),
            cloud_service=instanced.cloud_service,
            base_url=instanced.base_url,
        )

    def _attribute(
        self,
        base: Binding,
        attr: str,
        stack: set[tuple[str, str, str]] | None = None,
        depth: int = 0,
        *,
        enclosing: str = "",
        use: tuple[int, int] | None = None,
    ) -> Binding:
        if depth > _RESOLVE_DEPTH:
            return UNKNOWN
        if base.kind == "instance_external" and base.target:
            chosen, rest = self._types_defining(base, attr)
            typed = self._library().instance_attribute(chosen, attr)
            alt_types = []
            for target in rest:
                other = self._library().instance_attribute(target, attr)
                if other and other != typed:
                    alt_types.append(other)
            if typed:
                return Binding(
                    "instance_external",
                    typed,
                    confidence=min_confidence(base.confidence, "medium"),
                    cloud_service=base.cloud_service,
                    base_url=base.base_url,
                    fallback=_fluent(base, attr),
                    alternatives=tuple(dict.fromkeys(alt_types)),
                )
            return Binding(
                "external_attr",
                f"{chosen}.{attr}",
                confidence=base.confidence,
                cloud_service=base.cloud_service,
                base_url=base.base_url,
                fallback=_fluent(base, attr),
                alternatives=tuple(f"{target}.{attr}" for target in rest),
            )
        if base.kind in {"external_module", "external_attr"}:
            return Binding(
                "external_attr",
                f"{base.target}.{attr}",
                confidence=base.confidence,
                cloud_service=base.cloud_service,
                base_url=base.base_url,
                fallback=_fluent(base, attr),
                alternatives=tuple(
                    f"{target}.{attr}" for target in base.alternatives if target and target != base.target
                ),
            )
        if base.kind == "instance_local":
            method = f"{base.qualname}.{attr}"
            unit = self.units.get(base.target)
            if unit is not None and method in unit.defs:
                return Binding("symbol", base.target, method, confidence=base.confidence)
            found = self._instance_field(
                base,
                attr,
                stack if stack is not None else set(),
                depth + 1,
                enclosing=enclosing,
                use=use,
            )
            if found is not None:
                return found
            return UNKNOWN
        if base.kind == "symbol":
            unit = self.units.get(base.target)
            defn = unit.defs.get(base.qualname) if unit is not None else None
            if defn is not None and defn.kind == "class":
                method = f"{base.qualname}.{attr}"
                if unit is not None and method in unit.defs:
                    return Binding("symbol", base.target, method, confidence=base.confidence)
                found = self._instance_field(
                    Binding(
                        "instance_local",
                        base.target,
                        base.qualname,
                        confidence=base.confidence,
                    ),
                    attr,
                    stack if stack is not None else set(),
                    depth + 1,
                    enclosing=enclosing,
                    use=use,
                )
                if found is not None:
                    return found
            return UNKNOWN
        if base.kind == "module_local":
            binds = self._bindings_for(base.target)
            if attr in binds:
                return binds[attr]
            owner = self.units.get(base.target)
            if owner is not None:
                assigned = _visible_assign(owner, "<module>", attr, None)
                if assigned is not None and not _is_none(assigned):
                    return self._resolve_expr(
                        assigned,
                        owner,
                        "<module>",
                        stack if stack is not None else set(),
                        depth + 1,
                    )
            parent = _dotted(base.target)
            sub = self.index.lookup(f"{parent}.{attr}") if parent else None
            if sub:
                return Binding("module_local", sub, confidence=base.confidence)
            return UNKNOWN
        return UNKNOWN

    def _config_module_attr(
        self,
        name: str,
        attr: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
        depth: int,
    ) -> Binding | None:
        """Attribute on an imported config object that is a project module.

        ``from somewhere import settings`` may bind an external proxy. When a
        project module shares that name and assigns ``attr`` at module level,
        the attribute is that assignment. Several modules with the same stem
        are fine when only one of them writes the attribute.
        """
        if not name or not attr or not attr.isidentifier():
            return None
        bound = self._resolve_name(name, unit, enclosing, stack, depth + 1)
        if bound.kind not in {"unknown", "external_attr", "external_module"}:
            return None
        hits: list[str] = []
        for rel in self._modules_named(name):
            owner = self.units[rel]
            assigned = _visible_assign(owner, "<module>", attr, None)
            if assigned is not None and not _is_none(assigned):
                hits.append(rel)
        if len(hits) != 1:
            return None
        owner = self.units[hits[0]]
        assigned = _visible_assign(owner, "<module>", attr, None)
        if assigned is None:
            return None
        return self._resolve_expr(assigned, owner, "<module>", stack, depth + 1)

    def _modules_named(self, name: str) -> tuple[str, ...]:
        """Project modules whose file stem or package dir is ``name``."""
        if self._stem_index is None:
            index: dict[str, list[str]] = {}
            for rel in self.units:
                path = Path(rel)
                index.setdefault(path.stem, []).append(rel)
                if path.name == "__init__.py" and path.parent.name:
                    index.setdefault(path.parent.name, []).append(rel)
            self._stem_index = {key: tuple(vals) for key, vals in index.items()}
        return self._stem_index.get(name, ())

    def _is_import_local(self, unit: ModUnit, enclosing: str, name: str) -> bool:
        """True when ``name`` is introduced by an import in this scope chain."""
        if not name:
            return False
        for scope in (enclosing, *_enclosing_chain(enclosing), "<module>"):
            for raw in unit.local_imports.get(scope, ()):
                if raw.local == name:
                    return True
            if scope == "<module>":
                break
        return any(raw.local == name for raw in unit.imports)

    def _types_defining(self, base: Binding, attr: str) -> tuple[str, list[str]]:
        """The preferred type, then the other types that also define ``attr``.

        With one type this is that type and an empty rest, which is what a
        call resolved to before alternatives existed. A later type is used
        only when the preferred one does not define the attribute.
        """
        if not base.alternatives:
            return base.target, []
        targets = [base.target, *[item for item in base.alternatives if item and item != base.target]]
        library = self._library()
        defined = [target for target in targets if library.has_member(target, attr)]
        if not defined:
            return base.target, []
        chosen = base.target if base.target in defined else defined[0]
        return chosen, [target for target in defined if target != chosen]

    def _subscript_binding(self, base: Binding) -> Binding | None:
        """What ``base[key]`` produces, trying each type that can reach ``base``."""
        targets = [base.target, *[item for item in base.alternatives if item and item != base.target]]
        chosen = ""
        classes: tuple[str, ...] = ()
        alt_classes: list[str] = []
        library = self._library()
        for target in targets:
            found = library.call_return(f"{target}.__getitem__")
            if not found:
                continue
            if not chosen:
                chosen = target
                classes = found
            elif found[0] not in classes and found[0] not in alt_classes:
                alt_classes.append(found[0])
        if classes:
            return Binding(
                "instance_external",
                classes[0],
                confidence=min_confidence(base.confidence, "medium"),
                cloud_service=base.cloud_service,
                base_url=base.base_url,
                fallback=_fluent(base, "__getitem__"),
                alternatives=tuple(alt_classes),
            )
        return Binding(
            "instance_external",
            f"{base.target}.__getitem__",
            confidence=min_confidence(base.confidence, "medium"),
            cloud_service=base.cloud_service,
            base_url=base.base_url,
        )

    def _instance_field(
        self,
        base: Binding,
        attr: str,
        stack: set[tuple[str, str, str]],
        depth: int = 0,
        *,
        enclosing: str = "",
        use: tuple[int, int] | None = None,
    ) -> Binding | None:
        """A field assigned on this class or one of its bases.

        The use position picks the assignment that reaches a read in the same
        method. A read in another method sees every method's final assignment.
        """
        if depth > _RESOLVE_DEPTH:
            return None
        queue = [(base.target, base.qualname)]
        seen: set[tuple[str, str]] = set()
        while queue:
            rel, qual = queue.pop(0)
            if not rel or not qual or (rel, qual) in seen:
                continue
            seen.add((rel, qual))
            unit = self.units.get(rel)
            if unit is None:
                continue
            position = "" if use is None else f"{use[0]}:{use[1]}"
            token = ("field", rel, qual, attr, position)
            if token not in stack:
                stack.add(token)
                try:
                    chosen = _reaching_instance_exprs(unit, qual, attr, enclosing, use)
                    if chosen is not None:
                        if not chosen:
                            return None
                        found: list[Binding] = []
                        for expr, scope in chosen:
                            resolved = self._resolve_expr(expr, unit, scope, stack, depth + 1)
                            if resolved.kind == "external_attr":
                                resolved = _as_instance(resolved)
                            if resolved.kind != "unknown" and resolved.target:
                                found.append(resolved)
                        merged = _merge_bindings(found)
                        if merged is not None:
                            return merged
                    announced = self._annotated_binding(unit, qual, attr, stack, depth)
                    if announced is not None:
                        return announced
                finally:
                    stack.discard(token)
            for base_expr in unit.class_bases.get(qual, []):
                while isinstance(base_expr, ast.Subscript):
                    base_expr = base_expr.value
                parent = self._resolve_expr(base_expr, unit, qual, stack, depth + 1)
                if parent.kind in {"symbol", "instance_local"} and parent.qualname:
                    queue.append((parent.target, parent.qualname))
                    continue
                external = self._library_class_attr(parent, attr)
                if external is not None:
                    return external
        return None

    def _library_class_attr(self, parent: Binding, attr: str) -> Binding | None:
        """Attribute defined on an external class that this type inherits."""
        targets: list[str] = []
        if parent.kind in {"instance_external", "external_attr"} and parent.target:
            targets.append(parent.target)
            targets.extend(item for item in parent.alternatives if item and item != parent.target)
        if not targets:
            return None
        library = self._library()
        for target in targets:
            typed = library.instance_attribute(target, attr)
            if typed:
                return Binding(
                    "instance_external",
                    typed,
                    confidence=min_confidence(parent.confidence, "medium"),
                    cloud_service=parent.cloud_service,
                    base_url=parent.base_url,
                )
            if library.has_member(target, attr):
                owner = library.member_owner(target, attr) or target
                return Binding(
                    "external_attr",
                    f"{owner}.{attr}",
                    confidence=min_confidence(parent.confidence, "medium"),
                    cloud_service=parent.cloud_service,
                    base_url=parent.base_url,
                )
        return None

    def _call_result(
        self,
        func: Binding,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        cloud_service: str = "",
    ) -> Binding:
        if func.kind == "symbol":
            key = _sym(func.target, func.qualname)
            returned = self._returns.get(key)
            if returned is not None:
                return Binding(
                    returned.kind,
                    returned.target,
                    returned.qualname,
                    confidence=min_confidence(returned.confidence, "medium"),
                    cloud_service=returned.cloud_service or cloud_service,
                    base_url=returned.base_url,
                )
            owner = self.units.get(func.target)
            defn = owner.defs.get(func.qualname) if owner is not None else None
            if defn is not None and defn.kind == "class":
                return Binding(
                    "instance_local",
                    func.target,
                    func.qualname,
                    func.confidence,
                    cloud_service=cloud_service,
                )
            return UNKNOWN
        if func.kind in {"external_attr", "external_module", "instance_external"}:
            base_url = _base_url_from_call(node, unit, enclosing) or func.base_url
            qualname = func.target
            if func.kind == "instance_external":
                qualname = f"{func.target}.__call__"
            typed = self._library_instance(
                qualname, func, cloud_service, base_url, node, unit, enclosing
            )
            if typed is not None:
                return typed
            if func.kind == "instance_external":
                return UNKNOWN
            return Binding(
                "instance_external",
                func.target,
                confidence=func.confidence,
                cloud_service=cloud_service or func.cloud_service,
                base_url=base_url,
                fallback=func.fallback,
            )
        return UNKNOWN

    def _library_instance(
        self,
        qualname: str,
        func: Binding,
        cloud_service: str,
        base_url: str,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
    ) -> Binding | None:
        classes = self._library().call_return(qualname)
        if not classes:
            return None
        if classes[0].split(".")[-1] == qualname.split(".")[-1]:
            self._remember_init(classes[0], node, unit, enclosing)
        confidence = func.confidence
        if classes[0].split(".")[-1] != qualname.split(".")[-1]:
            confidence = min_confidence(confidence, "medium")
        return Binding(
            "instance_external",
            classes[0],
            confidence=confidence,
            cloud_service=cloud_service or func.cloud_service,
            base_url=base_url or func.base_url,
            fallback=func.fallback or func.target,
        )

    def _remember_init(
        self,
        qualname: str,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
    ) -> None:
        """Bind constructor parameters to the classes passed at this call."""
        params = self._library().init_params(qualname)
        if not params:
            return
        library = self._library()
        for name, arg in zip(params, node.args):
            self._remember_one_init(library, qualname, name, arg, unit, enclosing)
        for keyword in node.keywords:
            if keyword.arg and keyword.arg in params:
                self._remember_one_init(library, qualname, keyword.arg, keyword.value, unit, enclosing)

    def _remember_one_init(
        self,
        library: LibraryTypes,
        qualname: str,
        name: str,
        arg: ast.AST,
        unit: ModUnit,
        enclosing: str,
    ) -> None:
        resolved = self._resolve_expr(arg, unit, enclosing, set())
        if resolved.kind == "external_attr":
            resolved = _as_instance(resolved)
        if resolved.kind == "instance_external" and resolved.target:
            library.remember_init_arg(qualname, name, resolved.target)


def _public_names(unit: ModUnit, binds: dict[str, Binding]) -> list[str]:
    """Names a ``import *`` copies. ``__all__`` wins when it is a string list."""
    exported = unit.assigns.get(("<module>", "__all__"))
    if isinstance(exported, (ast.List, ast.Tuple)):
        names = [
            elt.value
            for elt in exported.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        if names:
            return [name for name in names if name in binds]
    return [name for name in binds if not name.startswith("_")]


def _absolute_module(rel: str, level: int, module: str) -> str:
    if level <= 0:
        return module
    parts = [part for part in _package_of(rel).split(".") if part]
    drop = level - 1
    if drop:
        parts = parts[:-drop] if drop <= len(parts) else []
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _local_def(unit: ModUnit, enclosing: str, name: str) -> str | None:
    prefix = "" if enclosing == "<module>" else enclosing + "."
    qual = prefix + name
    defn = unit.defs.get(qual)
    if defn is None:
        return None
    if enclosing == "<module>":
        return qual if "." not in qual else None
    parent = qual.rsplit(".", 1)[0]
    if parent == enclosing:
        return qual
    return None


def _params_of(unit: ModUnit, enclosing: str) -> set[str]:
    defn = unit.defs.get(enclosing)
    if defn is None:
        return set()
    return set(defn.params)


def _call_lineno(node: ast.Call) -> int:
    """Line of the method name when the call is ``receiver.method(...)``.

    The call expression can start on the receiver's line. The name being
    called is the last token of that attribute, which may be the next line.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return int(getattr(func, "end_lineno", 0) or getattr(node, "lineno", 0) or 0)
    return int(getattr(node, "lineno", 0) or 0)


def _end_pos(node: ast.AST) -> tuple[int, int]:
    end_line = int(getattr(node, "end_lineno", 0) or getattr(node, "lineno", 0) or 0)
    end_col = getattr(node, "end_col_offset", None)
    if end_col is None:
        end_col = getattr(node, "col_offset", 0) or 0
    return end_line, int(end_col)


# Language construction hooks, in the order they run. Not a package API.
_SETUP_METHODS = ("__new__", "__init__", "__post_init__")


def _method_name(scope: str) -> str:
    if not scope or scope == "<module>":
        return scope
    return scope.rsplit(".", 1)[-1]


def _reaching_instance_exprs(
    unit: ModUnit,
    qual: str,
    attr: str,
    enclosing: str,
    use: tuple[int, int] | None,
) -> list[tuple[ast.expr, str]] | None:
    """Assignments that reach a read of this field.

    ``None`` means this class never writes the field. An empty list means the
    write that reaches the read is ``None``. Otherwise the first item is the
    preferred type: inside the reading method, the latest write at or before
    the read; across methods, the latest write in ``__new__``, ``__init__``,
    or ``__post_init__``. Each other method contributes the value it leaves.
    """
    sites = list(unit.instance_assigns.get((qual, attr), []))
    if not sites:
        return None
    if use is not None and enclosing:
        prior = [item for item in sites if item[3] == enclosing and (item[0], item[1]) <= use]
        if prior:
            expr = prior[-1][2]
            if _is_none(expr):
                return []
            return [(expr, prior[-1][3])]
        # This method has not written the field yet, so its later writes do not reach here.
        sites = [item for item in sites if item[3] != enclosing]
        if not sites:
            return None
    by_scope: dict[str, tuple[int, int, ast.expr, str]] = {}
    for item in sites:
        if _is_none(item[2]):
            continue
        by_scope[item[3]] = item
    if not by_scope:
        return []
    preferred: tuple[int, int, ast.expr, str] | None = None
    for name in _SETUP_METHODS:
        item = by_scope.get(f"{qual}.{name}")
        if item is not None:
            preferred = item
    ordered: list[tuple[ast.expr, str]] = []
    if preferred is not None:
        ordered.append((preferred[2], preferred[3]))
    for scope, item in by_scope.items():
        if preferred is not None and scope == preferred[3]:
            continue
        if _method_name(scope) in _SETUP_METHODS:
            continue
        ordered.append((item[2], item[3]))
    if not ordered:
        return []
    return ordered


def _visible_assign(
    unit: ModUnit,
    enclosing: str,
    name: str,
    use: tuple[int, int] | None,
) -> ast.expr | None:
    """The assignment that reaches ``use``.

    A later reassignment does not hide the value a name has at an earlier use,
    and the right-hand side of ``name = name.method()`` still sees the previous
    value. ``end_col_offset`` is exclusive, so a use that starts at that column
    is after the binding.
    """
    sites = unit.assign_sites.get((enclosing, name))
    if use is not None and sites:
        visible = [item for item in sites if (item[0], item[1]) <= use]
        if not visible:
            return None
        return visible[-1][2]
    return unit.assigns.get((enclosing, name))


def _fluent_call_steps(
    expr: ast.expr, name: str
) -> tuple[ast.Name, list[ast.Call]] | None:
    """Peel ``name.m1().m2()`` into the root name and the calls, innermost first.

    Empty when the expression is not a fluent chain rooted at ``name``.
    """
    steps: list[ast.Call] = []
    cur: ast.AST = expr
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        steps.append(cur)
        cur = cur.func.value
    if not isinstance(cur, ast.Name) or cur.id != name or not steps:
        return None
    steps.reverse()
    return cur, steps


def _is_none(node: ast.AST) -> bool:
    """``x = None`` in an except clause is the missing-import fallback, not a binding."""
    return isinstance(node, ast.Constant) and node.value is None


def _fluent(base: Binding, attr: str) -> str:
    if not base.fallback:
        return ""
    return f"{base.fallback}.{attr}"


def _is_context_manager(defn: Def) -> bool:
    node = defn.node
    for deco in getattr(node, "decorator_list", []) or []:
        current = deco.func if isinstance(deco, ast.Call) else deco
        name = ""
        if isinstance(current, ast.Name):
            name = current.id
        elif isinstance(current, ast.Attribute):
            name = current.attr
        if name == "contextmanager" or name.endswith("contextmanager"):
            return True
    return False


def _as_instance(binding: Binding) -> Binding:
    if binding.kind == "external_attr":
        return Binding(
            "instance_external",
            binding.target,
            confidence=binding.confidence,
            cloud_service=binding.cloud_service,
            fallback=binding.fallback,
            alternatives=binding.alternatives,
        )
    if binding.kind == "symbol":
        return Binding(
            "instance_local",
            binding.target,
            binding.qualname,
            confidence=binding.confidence,
            cloud_service=binding.cloud_service,
        )
    if binding.kind in {"instance_external", "instance_local"}:
        return binding
    return UNKNOWN


def _call_bindings(binding: Binding) -> list[Binding]:
    """One binding per type that can reach this call.

    The preferred type stays first. The others are the rest of the union.
    """
    if not binding.alternatives:
        return [binding]
    found: list[Binding] = []
    seen: set[str] = set()
    for index, target in enumerate((binding.target, *binding.alternatives)):
        if not target or target in seen:
            continue
        seen.add(target)
        if index == 0:
            found.append(
                Binding(
                    binding.kind,
                    binding.target,
                    binding.qualname,
                    confidence=binding.confidence,
                    cloud_service=binding.cloud_service,
                    base_url=binding.base_url,
                    fallback=binding.fallback,
                )
            )
        else:
            found.append(
                Binding(
                    binding.kind,
                    target,
                    confidence=min_confidence(binding.confidence, "medium"),
                    cloud_service=binding.cloud_service,
                    base_url=binding.base_url,
                    fallback=binding.fallback,
                )
            )
    return found or [binding]


def _merge_bindings(found: list[Binding]) -> Binding | None:
    """First binding wins. Later targets of the same kind are alternatives."""
    flat: list[Binding] = []
    for binding in found:
        if binding.kind == "unknown" or not binding.target:
            continue
        pieces = [binding]
        if binding.alternatives:
            pieces = [
                Binding(
                    binding.kind,
                    binding.target,
                    binding.qualname,
                    confidence=binding.confidence,
                    cloud_service=binding.cloud_service,
                    base_url=binding.base_url,
                    fallback=binding.fallback,
                ),
                *[
                    Binding(
                        binding.kind,
                        alt,
                        confidence=min_confidence(binding.confidence, "medium"),
                        cloud_service=binding.cloud_service,
                        base_url=binding.base_url,
                        fallback=binding.fallback,
                    )
                    for alt in binding.alternatives
                    if alt and alt != binding.target
                ],
            ]
        for item in pieces:
            if any(prev.kind == item.kind and prev.target == item.target for prev in flat):
                continue
            flat.append(item)
    if not flat:
        return None
    primary = flat[0]
    alts = tuple(item.target for item in flat[1:] if item.kind == primary.kind)
    if not alts:
        return primary
    return Binding(
        primary.kind,
        primary.target,
        primary.qualname,
        confidence=primary.confidence,
        cloud_service=primary.cloud_service,
        base_url=primary.base_url,
        fallback=primary.fallback,
        alternatives=alts,
    )


def _delegated_binding(
    resolver: _Resolver,
    binding: Binding,
    specs: tuple[ServiceSpec, ...],
) -> Binding | None:
    """Retarget a wrapper call at the service its body actually calls.

    Only when a previous classification already showed this package does no
    I/O of its own and named the dependency. Nothing is fetched here.
    """
    if binding.kind not in {"external_attr", "external_module", "instance_external"}:
        return None
    if not binding.target or service_for_module(binding.target, specs) is not None:
        return None
    root = binding.target.split(".", 1)[0]
    found = cached_classification(root)
    if found is None or found.service or not found.delegates:
        return None
    allowed = {name.replace("-", "_") for name in found.delegates}
    allowed.update(found.delegates)
    for target in resolver._library().delegated_calls(binding.target):
        head = target.split(".", 1)[0]
        if head not in allowed and head.replace("-", "_") not in allowed:
            continue
        spec = service_for_module(target, specs)
        if spec is None or spec.transport:
            continue
        return Binding(
            "external_attr",
            target,
            confidence=min_confidence(binding.confidence, "medium"),
            cloud_service=binding.cloud_service,
            base_url=binding.base_url,
        )
    return None


def _service_of(binding: Binding, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    if binding.kind not in {"external_attr", "external_module", "instance_external"}:
        return None
    spec = service_for_module(binding.target, specs)
    if spec is None or spec.transport:
        return None
    return spec


def _kind_prefix(spec: ServiceSpec) -> str:
    if spec.kind == "database":
        return "db"
    if spec.kind == "cloud":
        return "cloud"
    return "sdk"


_FACTORY_TAILS = frozenset({"client", "resource"})


def _cloud_factory_roots(specs: tuple[ServiceSpec, ...]) -> frozenset[str]:
    """Import roots of cloud SDKs whose code ships a service model."""
    roots: set[str] = set()
    for spec in specs:
        if spec.kind != "cloud" or spec.transport:
            continue
        roots.update(spec.modules)
        roots.update(name.split(".")[0] for name in spec.modules if name)
        roots.update(prefix.split(".")[0] for prefix in spec.module_prefixes if prefix)
    return frozenset(roots)


def _factory_service(
    func: Binding,
    node: ast.Call,
    unit: ModUnit,
    enclosing: str,
    cloud_roots: frozenset[str] | None = None,
) -> str:
    """Literal service name passed to a cloud ``client`` / ``resource`` factory."""
    if func.kind not in {"external_attr", "external_module", "instance_external"}:
        return ""
    parts = func.target.split(".")
    if not parts or parts[-1] not in _FACTORY_TAILS:
        return ""
    root = parts[0]
    roots = cloud_roots or frozenset()
    if root not in roots:
        return ""
    if not node.args:
        return ""
    found = _const_str(node.args[0], unit, enclosing)
    return found.strip() if found else ""


def _symbol_target(
    binding: Binding, units: dict[str, ModUnit]
) -> tuple[str, str] | None:
    if binding.kind != "symbol":
        return None
    unit = units.get(binding.target)
    if unit is None or binding.qualname not in unit.defs:
        return None
    defn = unit.defs[binding.qualname]
    if defn.kind == "class":
        return None
    return binding.target, binding.qualname


def _sdk_kind(spec: ServiceSpec, binding: Binding) -> str:
    tail = binding.target.split(".")[-1]
    prefix = _kind_prefix(spec)
    if tail in spec.constructor_tails and binding.target.count(".") <= 2:
        return f"{prefix}_constructor"
    return f"{prefix}_call"


def _cloud_method(
    spec: ServiceSpec,
    binding: Binding,
    call: CallRec,
    unit: ModUnit,
    cloud_roots: frozenset[str],
) -> str:
    cloud = binding.cloud_service or _factory_service(
        binding, call.node, unit, call.enclosing, cloud_roots
    )
    if spec.kind == "cloud" and cloud:
        tail = binding.target.split(".")[-1]
        return f"{cloud}.{tail}"
    return binding.target


def _is_partial(binding: Binding) -> bool:
    target = binding.target or ""
    return target == "functools.partial" or target.endswith(".functools.partial") or (
        target.endswith("functools.partial")
    )


def _enclosing_chain(enclosing: str) -> list[str]:
    """Inner scope first, then each enclosing function, then the module."""
    if enclosing == "<module>":
        return ["<module>"]
    parts = [part for part in enclosing.split(".") if part]
    chain = [".".join(parts[: index + 1]) for index in range(len(parts) - 1, -1, -1)]
    chain.append("<module>")
    return chain


#: Helpers that take an HTTP verb and a path. The names are the call shape
#: shared by generated clients. A name one scanned distribution coins is not
#: listed here; a call that passes a verb and a path is matched below only
#: when the helper is one of these generic tails.
_SDK_REQUEST_TAILS = frozenset(
    {
        "request",
        "_request",
        "_static_request",
        "param_serialize",
        "call_api",
    }
)


def _guarded_host(unit: ModUnit, call: CallRec) -> str:
    """Host named by a check that selects this HTTP call.

    ``host.endswith("hooks.example.com")``, ``host == "..."``, and
    ``"..." in host`` count. The call has to sit in that branch, or in
    the function body outside any ``if``, when the function names one host.
    A host already matched from evidence is left to the literal-URL path.
    """
    scope = _scope_node(unit, call.enclosing)
    found: list[str] = []
    state = "miss"

    def walk(node: ast.AST, inherited: list[str], inside_if: bool) -> bool:
        nonlocal state
        if node is call.node:
            if inherited:
                found.extend(inherited)
                state = "branch"
            elif not inside_if:
                state = "open"
            else:
                state = "other"
            return True
        if isinstance(node, ast.If):
            test_hosts = _hosts_in_expr(node.test, unit, call.enclosing)
            active = test_hosts or inherited
            for stmt in node.body:
                if walk(stmt, active, True):
                    return True
            for stmt in node.orelse:
                if walk(stmt, inherited, True):
                    return True
            return False
        for child in ast.iter_child_nodes(node):
            if walk(child, inherited, inside_if):
                return True
        return False

    walk(scope, [], False)
    if state == "branch":
        return _pick_host(found)
    if state != "open":
        return ""
    hosts = _hosts_in_tree(scope, unit, call.enclosing)
    if len(set(hosts)) != 1:
        return ""
    return _pick_host(hosts)


def _hosts_in_tree(node: ast.AST, unit: ModUnit, enclosing: str) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.If):
            found.extend(_hosts_in_expr(child.test, unit, enclosing))
    return found


def _hosts_in_expr(node: ast.AST, unit: ModUnit, enclosing: str) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == "endswith" and child.args:
                host = _host_token(_const_str(child.args[0], unit, enclosing))
                if host:
                    found.append(host)
            continue
        if not isinstance(child, ast.Compare):
            continue
        for op, comp in zip(child.ops, child.comparators):
            if not isinstance(op, (ast.Eq, ast.In)):
                continue
            for side in (child.left, comp):
                host = _host_token(_const_str(side, unit, enclosing))
                if host:
                    found.append(host)
    return found


def _host_token(text: str | None) -> str:
    if not text:
        return ""
    token = text.strip().lower()
    if token.startswith(("http://", "https://")):
        return (urlparse(token).hostname or "").lower()
    token = token.strip(".")
    if not token or any(char in token for char in " /:\\"):
        return ""
    labels = token.split(".")
    if len(labels) < 2:
        return ""
    if any(not re.fullmatch(r"[a-z0-9-]+", label) or not label for label in labels):
        return ""
    if not re.fullmatch(r"[a-z]{2,}", labels[-1]):
        return ""
    return token


def _pick_host(hosts: list[str]) -> str:
    unique = list(dict.fromkeys(host for host in hosts if host))
    if not unique:
        return ""
    unique.sort(key=len, reverse=True)
    return unique[0]


_RESERVED_HOST_TAILS = frozenset({"example", "invalid", "test", "localhost"})


def _package_label(host: str) -> str:
    """Label immediately before the TLD of a hostname."""
    labels = [part for part in (host or "").lower().split(".") if part]
    if len(labels) < 2 or labels[-1] in _RESERVED_HOST_TAILS:
        return ""
    return labels[-2]


def _spec_for_literal_host(
    host: str,
    specs: tuple[ServiceSpec, ...],
    root: Path,
) -> ServiceSpec | None:
    """Attribute a URL host to the distribution whose own code names it.

    The label before the TLD is classified. It is used only when that
    distribution's hosts actually contain this host. Otherwise the host is
    its own service, so the call is still mapped. Reserved TLDs are dropped.
    """
    found = service_for_host(host, specs)
    if found is not None:
        return found
    label = _package_label(host)
    if label:
        spec = evidence_spec(label, root=root)
        if spec is not None and service_for_host(host, (spec,)):
            return spec
    # A two-label host such as a documentation or health URL is not a service.
    # Three or more labels match the same bar used when reading hosts out of a wheel.
    labels = [part for part in host.lower().split(".") if part]
    if len(labels) < 3 or labels[-1] in _RESERVED_HOST_TAILS:
        return None
    return ServiceSpec(service_id=host, package=host, modules=(), hosts=(host,))


def _http_service(
    binding: Binding,
    call: CallRec,
    unit: ModUnit,
    specs: tuple[ServiceSpec, ...],
    root: Path,
) -> tuple[ServiceSpec, str, str] | None:
    if binding.kind not in {"external_attr", "instance_external"}:
        return None
    parts = binding.target.split(".")
    if not parts:
        return None
    # A generic HTTP library, or the stdlib URL stack. A literal URL still
    # needs a service that names that host. A host named only by a guard
    # (``endswith``, ``==``, ``in``) is its own service when none does.
    http_spec = service_for_module(binding.target, specs)
    stdlib_http = parts[0] in {"urllib", "http"}
    if (
        binding.kind == "external_attr"
        and parts[-1] in HTTP_METHOD_TAILS
        and (stdlib_http or (http_spec is not None and http_spec.transport))
    ):
        verb = parts[-1].upper()
        if verb == "REQUEST":
            verb = _request_verb(call.node) or "GET"
        if verb == "URLOPEN":
            verb = "GET"
        url = _call_url(call.node, unit, call.enclosing, binding.base_url)
        if url:
            host = (urlparse(url).hostname or "").lower()
            spec = _spec_for_literal_host(host, specs, root)
            if spec is None:
                return None
            return spec, f"{verb} {url}", "high"
        host = _guarded_host(unit, call)
        if not host or service_for_host(host, specs) is not None:
            return None
        spec = ServiceSpec(
            service_id=host,
            package=host,
            modules=(),
            hosts=(host,),
        )
        return spec, f"{verb} https://{host}", "medium"
    # A helper that is handed an HTTP verb and an API path.
    return _sdk_path_request(binding, call, unit, specs)


def _sdk_path_request(
    binding: Binding,
    call: CallRec,
    unit: ModUnit,
    specs: tuple[ServiceSpec, ...],
) -> tuple[ServiceSpec, str, str] | None:
    parts = binding.target.split(".")
    if not parts or parts[-1] not in _SDK_REQUEST_TAILS:
        return None
    spec = service_for_module(binding.target, specs)
    if spec is None and unit.rel:
        # Generated client methods often call self.api_client.param_serialize;
        # the module path still names the package (airflow_client.…).
        dotted = _dotted(unit.rel)
        spec = service_for_module(dotted, specs)
    if spec is None or not spec.hosts:
        return None
    verb, path = _sdk_request_verb_path(call.node, unit, call.enclosing)
    if not verb or not path:
        return None
    host = spec.hosts[0]
    url = f"https://{host}{path}" if path.startswith("/") else f"https://{host}/{path}"
    return spec, f"{verb} {url}", "high"


def _module_path_request(
    call: CallRec,
    unit: ModUnit,
    specs: tuple[ServiceSpec, ...],
) -> tuple[ServiceSpec, str, str] | None:
    """A generated client helper called with an HTTP verb and an API path."""
    func = call.node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _SDK_REQUEST_TAILS:
        return None
    if not unit.rel:
        return None
    verb, path = _sdk_request_verb_path(call.node, unit, call.enclosing)
    if not verb or not path:
        return None
    spec = service_for_module(_dotted(unit.rel), specs)
    if spec is not None and spec.hosts:
        host = spec.hosts[0]
    else:
        # Generated clients call param_serialize without a classified spec.
        # The enclosing package is the service; the path is the evidence.
        top = _dotted(unit.rel).split(".", 1)[0]
        if spec is None:
            spec = ServiceSpec(service_id=top, package=top.replace("_", "-"), modules=(top,))
        host = spec.hosts[0] if spec.hosts else top
    url = f"https://{host}{path}" if path.startswith("/") else f"https://{host}/{path}"
    return spec, f"{verb} {url}", "high"


def _sdk_request_verb_path(
    call: ast.Call, unit: ModUnit, enclosing: str
) -> tuple[str, str]:
    """``("post", "/v1/charges")`` from positional or keyword args."""
    strings: list[str] = []
    for arg in call.args:
        text = _const_str(arg, unit, enclosing)
        if text:
            strings.append(text)
    for keyword in call.keywords:
        if keyword.arg not in {None, "method", "url", "path", "resource_path"}:
            continue
        text = _const_str(keyword.value, unit, enclosing)
        if text:
            strings.append(text)
    verb = ""
    path = ""
    for text in strings:
        lowered = text.strip().lower()
        if not verb and lowered in {
            "get", "post", "put", "patch", "delete", "head", "options"
        }:
            verb = lowered.upper()
            continue
        if not path and _looks_like_api_path(text):
            path = text.split("?", 1)[0]
    return verb, path


def _looks_like_api_path(text: str) -> bool:
    token = (text or "").strip()
    if not token.startswith("/"):
        return False
    if "://" in token or " " in token:
        return False
    # OpenAPI-style path: /v1/..., /repos/..., /billing/...
    return bool(re.match(r"^/[A-Za-z0-9_./{}%-]+$", token))


def _request_verb(call: ast.Call) -> str:
    if not call.args:
        return ""
    node = call.args[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper()
    return ""


def _call_url(call: ast.Call, unit: ModUnit, enclosing: str, base_url: str = "") -> str | None:
    candidates: list[str] = []
    for arg in call.args:
        candidate = _const_str(arg, unit, enclosing)
        if candidate:
            candidates.append(candidate)
    for keyword in call.keywords:
        if keyword.arg not in {None, "url"}:
            continue
        candidate = _const_str(keyword.value, unit, enclosing)
        if candidate:
            candidates.append(candidate)
    for candidate in candidates:
        absolute = _join_url(base_url, candidate)
        if absolute.startswith(("http://", "https://")) and urlparse(absolute).hostname:
            return absolute
    return None


def _join_url(base: str, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    if not base:
        return url
    root = base if "://" in base else ""
    if not root:
        return url
    if url.startswith("/"):
        return root.rstrip("/") + url
    return root.rstrip("/") + "/" + url


def _base_url_from_call(call: ast.Call, unit: ModUnit, enclosing: str) -> str:
    for keyword in call.keywords:
        if keyword.arg not in {"base_url", "base"}:
            continue
        found = _const_str(keyword.value, unit, enclosing)
        if found and found.startswith(("http://", "https://")):
            return found
    return ""


def _const_str(node: ast.AST, unit: ModUnit, enclosing: str, depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                inner = _const_str(value.value, unit, enclosing, depth + 1)
                parts.append(inner if inner is not None else "{}")
                continue
            return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_str(node.left, unit, enclosing, depth + 1)
        right = _const_str(node.right, unit, enclosing, depth + 1)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # ``"/v1/items/%s/usage_records" % item_id`` → template with ``{}``.
        left = _const_str(node.left, unit, enclosing, depth + 1)
        if left is None:
            return None
        return re.sub(r"%(?:\([^)]+\))?[#0 +\-]*\d*(?:\.\d+)?[sdif]", "{}", left)
    if isinstance(node, ast.Name):
        cached = unit.constants.get((enclosing, node.id)) or unit.constants.get(("<module>", node.id))
        if cached:
            return cached
        assigned = unit.assigns.get((enclosing, node.id)) or unit.assigns.get(("<module>", node.id))
        if assigned is not None:
            return _const_str(assigned, unit, enclosing, depth + 1)
    return None


def _http_payload_keys(call: ast.Call, unit: ModUnit, enclosing: str) -> tuple[str, ...]:
    keys: list[str] = []
    for keyword in call.keywords:
        if keyword.arg not in {"json", "data", "params"}:
            continue
        keys.extend(_dict_keys(keyword.value, unit, enclosing))
    return tuple(dict.fromkeys(keys))


def _dict_introductions(
    value: ast.expr, dict_name: str, lineno: int
) -> list[tuple[str, int, str]]:
    """Keys introduced by a dict literal or ``dict(...)`` constructor."""
    found: list[tuple[str, int, str]] = []
    if isinstance(value, ast.Dict):
        for key in value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append((key.value, lineno, f"{dict_name}[{key.value!r}]"))
        return found
    if isinstance(value, ast.Call) and _is_dict_constructor(value):
        for keyword in value.keywords:
            if keyword.arg:
                found.append(
                    (keyword.arg, lineno, f"{dict_name} = dict({keyword.arg}=...)")
                )
        for arg in value.args:
            if isinstance(arg, ast.Dict):
                for key in arg.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((key.value, lineno, f"{dict_name} = dict(...)"))
    return found


def _update_introductions(
    node: ast.Call,
    dict_name: str,
    lineno: int,
    unit: ModUnit | None = None,
    enclosing: str = "",
) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for keyword in node.keywords:
        if keyword.arg:
            found.append(
                (keyword.arg, lineno, f"{dict_name}.update({keyword.arg}=...)")
            )
    for arg in node.args:
        if isinstance(arg, ast.Dict):
            for key in arg.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.append(
                        (key.value, lineno, f"{dict_name}.update({{{key.value!r}: ...}})")
                    )
            continue
        if unit is None:
            continue
        for key, _line, _expr in _dict_key_origins(arg, unit, enclosing):
            found.append(
                (key, lineno, f"{dict_name}.update({{{key!r}: ...}})")
            )
    return found


def _is_dict_constructor(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id == "dict"
    return isinstance(node.func, ast.Attribute) and node.func.attr == "dict"


def _dict_keys(node: ast.AST, unit: ModUnit, enclosing: str, depth: int = 0) -> list[str]:
    return [key for key, _line, _expr in _dict_key_origins(node, unit, enclosing, depth)]


def _dict_key_origins(
    node: ast.AST, unit: ModUnit, enclosing: str, depth: int = 0
) -> list[tuple[str, int, str]]:
    if depth > 4:
        return []
    if isinstance(node, ast.Dict):
        lineno = getattr(node, "lineno", 0) or 0
        return [
            (key.value, lineno, f"{{{key.value!r}: ...}}")
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
    if isinstance(node, ast.Call) and _is_dict_constructor(node):
        lineno = getattr(node, "lineno", 0) or 0
        return _dict_introductions(node, "dict", lineno)
    if isinstance(node, ast.Name):
        found: list[tuple[str, int, str]] = []
        found.extend(unit.dict_key_origins.get((enclosing, node.id), []))
        found.extend(unit.dict_key_origins.get(("<module>", node.id), []))
        assigned = unit.assigns.get((enclosing, node.id)) or unit.assigns.get(
            ("<module>", node.id)
        )
        if assigned is not None:
            found.extend(_dict_key_origins(assigned, unit, enclosing, depth + 1))
        return _dedupe_origins(found)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        qual = _helper_qual(unit, enclosing, node.func.id)
        if qual is None:
            return []
        found = []
        for returned in unit.returns.get(qual, []):
            found.extend(_dict_key_origins(returned, unit, qual, depth + 1))
            if isinstance(returned, ast.Name):
                found.extend(unit.dict_key_origins.get((qual, returned.id), []))
        return _dedupe_origins(found)
    return []


def _helper_qual(unit: ModUnit, enclosing: str, name: str) -> str | None:
    """A nested def, or a same-module top-level function used as a local helper."""
    nested = _local_def(unit, enclosing, name)
    if nested is not None:
        return nested
    defn = unit.defs.get(name)
    if defn is not None and defn.kind == "function" and "." not in name:
        return name
    return None


def _dedupe_origins(
    origins: list[tuple[str, int, str]],
) -> list[tuple[str, int, str]]:
    seen: set[tuple[str, int, str]] = set()
    found: list[tuple[str, int, str]] = []
    for item in origins:
        if item in seen:
            continue
        seen.add(item)
        found.append(item)
    return found


def _make_direct(
    unit: ModUnit,
    call: CallRec,
    *,
    spec: ServiceSpec,
    sdk_method: str,
    kind: str,
    confidence: str,
) -> _Direct:
    scope = _scope_node(unit, call.enclosing)
    fields, returns_raw = _response_uses(scope, call.node)
    params, origins = _keyword_bundle(call.node, unit, call.enclosing)
    return _Direct(
        rel=unit.rel,
        enclosing=call.enclosing,
        lineno=call.lineno,
        expr=_expr(call.node),
        service=spec.service_id,
        package=spec.package,
        sdk_method=sdk_method,
        kind=kind,
        params=params,
        response_fields=fields,
        returns_raw=returns_raw,
        confidence=confidence,
        param_origins=origins,
    )


def _response_directs(unit: ModUnit, call: CallRec, direct: _Direct) -> list[_Direct]:
    """Attribute, dict, and index reads of a call's return value, including aliases."""
    scope = _scope_node(unit, call.enclosing)
    names = _response_names(scope, _assigned_names(scope, call.node))
    sites = _access_sites(scope, names)
    sites.extend(_inline_call_sites(scope, call.node))
    if not sites:
        return []
    kind = _response_kind(direct.kind)
    found: list[_Direct] = []
    seen: set[tuple[int, str]] = set()
    for lineno, expr, path in sites:
        key = (lineno, expr)
        if key in seen or not path:
            continue
        seen.add(key)
        found.append(
            _Direct(
                rel=direct.rel,
                enclosing=direct.enclosing,
                lineno=lineno,
                expr=expr,
                service=direct.service,
                package=direct.package,
                sdk_method=direct.sdk_method,
                kind=kind,
                params=(),
                response_fields=(path,),
                returns_raw=False,
                confidence=direct.confidence,
            )
        )
    return found


def _response_kind(kind: str) -> str:
    if kind.endswith("_call"):
        return kind[: -len("_call")] + "_response"
    if kind.endswith("_constructor"):
        return kind[: -len("_constructor")] + "_response"
    return kind + "_response"


def _rooted_at_call(node: ast.AST, call: ast.Call) -> bool:
    cur: ast.AST | None = node
    while isinstance(cur, (ast.Subscript, ast.Attribute)):
        cur = cur.value
    return cur is call


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (TypeError, ValueError):
        return "<expr>"


def _exception_refs(
    units: dict[str, ModUnit],
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
) -> list[_Direct]:
    """References to exception classes: except, isinstance, retry lists, attributes."""
    found: list[_Direct] = []
    seen: set[tuple[str, int, str]] = set()
    for rel, unit in units.items():
        callee_ids = _callee_ids(unit.tree)
        scan = _RefScan(unit, resolver, specs, callee_ids, found, seen)
        scan.visit(unit.tree)
        scan.flush_symbols()
    return found


def _callee_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        cur: ast.AST | None = node.func
        while isinstance(cur, (ast.Attribute, ast.Subscript, ast.Call, ast.Name)):
            found.add(id(cur))
            if isinstance(cur, ast.Call):
                cur = cur.func
            elif isinstance(cur, ast.Name):
                break
            else:
                cur = cur.value
    return found


def _rooted_on_package(node: ast.Attribute, resolver: _Resolver, unit: ModUnit, enclosing: str) -> bool:
    """True when the attribute chain starts at an imported package or symbol."""
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return False
    binding = resolver._resolve_name(cur.id, unit, enclosing, set())
    return binding.kind in {"external_module", "external_attr"}


def _exception_like(target: str) -> bool:
    parts = [part for part in target.split(".") if part]
    if not parts:
        return False
    tail = parts[-1]
    if not tail[:1].isupper():
        return False
    if tail.endswith("Error") or tail.endswith("Exception"):
        return True
    return any(part in {"error", "errors", "exceptions", "_exceptions"} for part in parts[:-1])


class _RefScan(ast.NodeVisitor):
    def __init__(
        self,
        unit: ModUnit,
        resolver: _Resolver,
        specs: tuple[ServiceSpec, ...],
        callee_ids: set[int],
        found: list[_Direct],
        seen: set[tuple[str, int, str]],
    ) -> None:
        self.unit = unit
        self.resolver = resolver
        self.specs = specs
        self.callee_ids = callee_ids
        self.found = found
        self.seen = seen
        self.enclosing = "<module>"
        self._symbols: list[tuple[ast.Attribute, Binding, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        previous = self.enclosing
        for deco in node.decorator_list:
            self.visit(deco)
        self.enclosing = node.name if previous == "<module>" else f"{previous}.{node.name}"
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.AST) -> None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        previous = self.enclosing
        for deco in node.decorator_list:
            self.visit(deco)
        for default in list(node.args.defaults) + [item for item in node.args.kw_defaults if item is not None]:
            self.visit(default)
        self.enclosing = node.name if previous == "<module>" else f"{previous}.{node.name}"
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing = previous

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            for expr in _atomic_exprs(node.type):
                self._record(expr, force=True)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == "isinstance" and len(node.args) >= 2:
            for expr in _atomic_exprs(node.args[1]):
                self._record(expr, force=True)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record(node, force=False)
            self._remember_symbol(node)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self._remember_symbol(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record(node, force=False)

    def _record(self, expr: ast.AST, *, force: bool) -> None:
        if id(expr) in self.callee_ids:
            return
        binding = self.resolver._resolve_expr(expr, self.unit, self.enclosing, set())
        spec = _service_of(binding, self.specs)
        if spec is None or not binding.target:
            return
        if not force and not _exception_like(binding.target):
            return
        if force and not _exception_like(binding.target) and binding.kind not in {
            "external_attr",
            "external_module",
            "instance_external",
        }:
            return
        key = (self.unit.rel, getattr(expr, "lineno", 0) or 0, binding.target)
        if key in self.seen:
            return
        self.seen.add(key)
        self.found.append(
            _Direct(
                rel=self.unit.rel,
                enclosing=self.enclosing,
                lineno=key[1],
                expr=_unparse(expr),
                service=spec.service_id,
                package=spec.package,
                sdk_method=binding.target,
                kind=f"{_kind_prefix(spec)}_ref",
                params=(),
                response_fields=(),
                returns_raw=False,
                confidence=binding.confidence or "high",
            )
        )

    def _remember_symbol(self, node: ast.Attribute) -> None:
        """A package attribute that is loaded or stored, not called.

        ``return pkg.Widget.create`` and ``pkg.api_base = ...`` name a symbol
        without calling it. Exception classes are already recorded.
        """
        if id(node) in self.callee_ids:
            return
        if not _rooted_on_package(node, self.resolver, self.unit, self.enclosing):
            return
        binding = self.resolver._resolve_expr(node, self.unit, self.enclosing, set())
        if binding.kind not in {"external_attr", "external_module"} or not binding.target:
            return
        if _exception_like(binding.target):
            return
        if binding.target.count(".") < 1:
            return
        self._symbols.append((node, binding, self.enclosing))

    def flush_symbols(self) -> None:
        inners: set[int] = set()
        for node, _binding, _enclosing in self._symbols:
            cur: ast.AST | None = node.value
            while isinstance(cur, ast.Attribute):
                inners.add(id(cur))
                cur = cur.value
        for node, binding, enclosing in self._symbols:
            if id(node) in inners:
                continue
            spec = _service_of(binding, self.specs)
            if spec is None:
                continue
            key = (self.unit.rel, getattr(node, "lineno", 0) or 0, binding.target)
            if key in self.seen:
                continue
            self.seen.add(key)
            self.found.append(
                _Direct(
                    rel=self.unit.rel,
                    enclosing=enclosing,
                    lineno=key[1],
                    expr=_unparse(node),
                    service=spec.service_id,
                    package=spec.package,
                    sdk_method=binding.target,
                    kind=f"{_kind_prefix(spec)}_ref",
                    params=(),
                    response_fields=(),
                    returns_raw=False,
                    confidence=binding.confidence or "high",
                )
            )


def _atomic_exprs(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        found: list[ast.AST] = []
        for elt in node.elts:
            found.extend(_atomic_exprs(elt))
        return found
    if isinstance(node, ast.Starred):
        return _atomic_exprs(node.value)
    return [node]


def _make_edge(
    unit: ModUnit,
    call: CallRec,
    callee_rel: str,
    callee: str,
    confidence: str,
) -> _Edge:
    scope = _scope_node(unit, call.enclosing)
    names = _response_names(scope, _assigned_names(scope, call.node))
    sites = tuple(_access_sites(scope, names))
    fields = tuple(dict.fromkeys(path for _lineno, _expr, path in sites if path))
    return _Edge(
        caller_rel=unit.rel,
        caller=call.enclosing,
        callee_rel=callee_rel,
        callee=callee,
        lineno=call.lineno,
        expr=_expr(call.node),
        keywords=_keywords(call.node),
        return_fields=fields,
        return_sites=sites,
        confidence=confidence,
    )


def _scope_node(unit: ModUnit, enclosing: str) -> ast.AST:
    if enclosing == "<module>":
        return unit.tree
    defn = unit.defs.get(enclosing)
    if defn is None:
        return unit.tree
    return defn.node


def _response_uses(scope: ast.AST, call: ast.Call) -> tuple[tuple[str, ...], bool]:
    names = _assigned_names(scope, call)
    raw = _returns_call(scope, call, names)
    flowed = _response_names(scope, names)
    if not flowed:
        return (), raw
    return _fields_of_names(scope, tuple(flowed)), raw


def _unwrap_await(node: ast.AST | None) -> ast.AST | None:
    while isinstance(node, ast.Await):
        node = node.value
    return node


#: Response-body helpers chained off an HTTP call (``resp.json()``, …).
_HTTP_BODY_TAILS = frozenset({"json", "text", "read", "content", "aread", "aiter_bytes"})


def _unwrap_http_body(value: ast.AST | None) -> ast.AST | None:
    """Strip ``await`` and ``.json()`` / ``.text()`` wrappers down to the HTTP call."""
    cur = _unwrap_await(value)
    for _ in range(4):
        if not isinstance(cur, ast.Call) or not isinstance(cur.func, ast.Attribute):
            break
        if cur.func.attr not in _HTTP_BODY_TAILS:
            break
        cur = _unwrap_await(cur.func.value)
    return cur


def _assigned_names(scope: ast.AST, call: ast.Call) -> tuple[str, ...]:
    names: list[str] = []
    for node in _iter_scope(scope):
        if isinstance(node, ast.Assign) and _unwrap_http_body(node.value) is call:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and _unwrap_http_body(node.value) is call
            and isinstance(node.target, ast.Name)
        ):
            names.append(node.target.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _unwrap_http_body(item.context_expr) is call and isinstance(
                    item.optional_vars, ast.Name
                ):
                    names.append(item.optional_vars.id)
    return tuple(dict.fromkeys(names))


def _response_names(scope: ast.AST, seeds: tuple[str, ...] | set[str]) -> set[str]:
    """Names that hold the response, following attribute and subscript assignments."""
    known = set(seeds)
    if not known:
        return known
    for _ in range(12):
        changed = False
        for node in _iter_scope(scope):
            if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
                if node.target.id not in known:
                    root = _iter_root(node.iter)
                    if root is not None and root in known:
                        known.add(node.target.id)
                        changed = True
            target, value = _assign_target_value(node)
            if not isinstance(target, ast.Name) or value is None or target.id in known:
                continue
            if isinstance(value, ast.Name) and value.id in known:
                known.add(target.id)
                changed = True
                continue
            if isinstance(value, (ast.Attribute, ast.Subscript)):
                parsed = _use_path(value)
                if parsed is not None and parsed[0] in known:
                    known.add(target.id)
                    changed = True
                    continue
            # ``data = resp.json()`` keeps the same response object for field reads.
            body = _unwrap_http_body(value)
            if isinstance(body, ast.Name) and body.id in known:
                known.add(target.id)
                changed = True
        if not changed:
            break
    return known


def _iter_root(node: ast.AST) -> str | None:
    """Name a ``for`` walks when the iterable is a response or a slice of one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        parsed = _use_path(node)
        return parsed[0] if parsed else None
    if isinstance(node, ast.Call) and node.args:
        return _iter_root(node.args[0])
    return None


def _assign_target_value(node: ast.AST) -> tuple[ast.AST | None, ast.AST | None]:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0], node.value
    if isinstance(node, ast.AnnAssign):
        return node.target, node.value
    return None, None


def _access_sites(scope: ast.AST, names: set[str]) -> list[tuple[int, str, str]]:
    if not names:
        return []
    skip = _call_func_ids(scope)
    nodes = [
        node
        for node in _iter_scope(scope)
        if isinstance(node, (ast.Attribute, ast.Subscript)) and id(node) not in skip
    ]
    inner = {
        id(node.value)
        for node in nodes
        if isinstance(node.value, (ast.Attribute, ast.Subscript))
    }
    found: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for node in nodes:
        if id(node) in inner:
            continue
        parsed = _use_path(node)
        if parsed is None:
            continue
        root, path = parsed
        if root not in names or not path:
            continue
        expr = _unparse(node)
        key = (getattr(node, "lineno", 0) or 0, expr)
        if key in seen:
            continue
        seen.add(key)
        found.append((key[0], expr, path))
    for node in _iter_scope(scope):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not isinstance(receiver, (ast.Attribute, ast.Subscript)):
            continue
        parsed = _use_path(receiver)
        if parsed is None:
            continue
        root, path = parsed
        if root not in names or not path:
            continue
        expr = _unparse(receiver)
        key = (getattr(receiver, "lineno", 0) or 0, expr)
        if key in seen:
            continue
        seen.add(key)
        found.append((key[0], expr, path))
    return found


def _inline_call_sites(scope: ast.AST, call: ast.Call) -> list[tuple[int, str, str]]:
    skip = _call_func_ids(scope)
    nodes = [
        node
        for node in _iter_scope(scope)
        if isinstance(node, (ast.Attribute, ast.Subscript)) and id(node) not in skip
    ]
    rooted = [node for node in nodes if _rooted_at_call(node, call)]
    inner = {id(node.value) for node in rooted if isinstance(node.value, (ast.Attribute, ast.Subscript))}
    found: list[tuple[int, str, str]] = []
    for node in rooted:
        if id(node) in inner:
            continue
        path = _path_from_call(node, call)
        if not path:
            continue
        found.append((getattr(node, "lineno", 0) or 0, _unparse(node), path))
    return found


def _path_from_call(node: ast.AST, call: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while cur is not call and isinstance(cur, (ast.Attribute, ast.Subscript)):
        if isinstance(cur, ast.Attribute):
            parts.append("." + cur.attr)
        else:
            parts.append(_slice_text(cur.slice))
        cur = cur.value
    if cur is not call:
        return ""
    parts.reverse()
    return "".join(parts)


def _caller_response_chains(direct: _Direct, chain: Chain, edge: _Edge) -> list[Chain]:
    """Field reads in a caller when the callee returns the SDK response."""
    kind = _response_kind(direct.kind)
    found: list[Chain] = []
    for lineno, expr, path in edge.return_sites:
        if not path:
            continue
        hop = Hop(edge.caller_rel, lineno, edge.caller, expr)
        found.append(
            Chain(
                service=direct.service,
                package=direct.package,
                call_rel=edge.caller_rel,
                call_lineno=lineno,
                call_symbol=edge.caller,
                call_expr=expr,
                hops=(hop,) + chain.hops,
                sdk_method=direct.sdk_method,
                kind=kind,
                params=(),
                site_keywords=(),
                response_fields=(path,),
                confidence=min_confidence(chain.confidence, edge.confidence),
                via_wrapper=False,
            )
        )
    return found


def _returns_call(scope: ast.AST, call: ast.Call, names: tuple[str, ...]) -> bool:
    name_set = set(names)
    for node in _iter_scope(scope):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if _unwrap_await(node.value) is call:
            return True
        if isinstance(node.value, ast.Name) and node.value.id in name_set:
            return True
    return False


def _fields_of_names(scope: ast.AST, names: tuple[str, ...]) -> tuple[str, ...]:
    wanted = set(names)
    skip = _call_func_ids(scope)
    found: list[str] = []
    for node in _iter_scope(scope):
        if not isinstance(node, (ast.Attribute, ast.Subscript)):
            continue
        if id(node) in skip:
            continue
        parsed = _use_path(node)
        if parsed is None:
            continue
        root, path = parsed
        if root in wanted and path:
            found.append(path)
    return _maximal(found)


def _call_func_ids(scope: ast.AST) -> set[int]:
    """Attribute/subscript nodes that are the callee of a call, not a read."""
    found: set[int] = set()
    for node in _iter_scope(scope):
        if not isinstance(node, ast.Call):
            continue
        cur: ast.AST | None = node.func
        while isinstance(cur, (ast.Attribute, ast.Subscript, ast.Call)):
            found.add(id(cur))
            cur = cur.func if isinstance(cur, ast.Call) else cur.value
    return found


def _use_path(node: ast.AST) -> tuple[str, str] | None:
    parts: list[str] = []
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.Attribute):
            parts.append("." + cur.attr)
            cur = cur.value
            continue
        if isinstance(cur, ast.Subscript):
            parts.append(_slice_text(cur.slice))
            cur = cur.value
            continue
        if isinstance(cur, ast.Name):
            parts.reverse()
            return cur.id, "".join(parts)
        return None
    return None


def _slice_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return "[" + repr(node.value) + "]"
        if isinstance(node.value, int):
            return f"[{node.value}]"
    return "[*]"


def _maximal(paths: list[str]) -> tuple[str, ...]:
    unique = list(dict.fromkeys(paths))
    kept = [
        path
        for path in unique
        if not any(
            other != path and other.startswith(path) and len(other) > len(path)
            for other in unique
        )
    ]
    return tuple(kept)


def _iter_scope(root: ast.AST):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)


def _keywords(call: ast.Call, unit: ModUnit | None = None, enclosing: str = "") -> tuple[str, ...]:
    names, _origins = _keyword_bundle(call, unit, enclosing)
    return names


def _keyword_bundle(
    call: ast.Call, unit: ModUnit | None = None, enclosing: str = ""
) -> tuple[tuple[str, ...], tuple[tuple[str, int, str], ...]]:
    """Explicit keywords, ``**`` splat keys, and ``functools.partial`` forwards.

    Origins are only the splat / partial introductions — not keywords written
    directly on the SDK call (that line is already the finding).
    """
    names = [kw.arg for kw in call.keywords if kw.arg]
    origins: list[tuple[str, int, str]] = []
    if unit is None:
        return tuple(dict.fromkeys(names)), ()
    for key, lineno, expr in _partial_kw_origins(call, unit, enclosing):
        names.append(key)
        origins.append((key, lineno, expr))
    for kw in call.keywords:
        if kw.arg is not None:
            continue
        for key, lineno, expr in _dict_key_origins(kw.value, unit, enclosing):
            names.append(key)
            origins.append((key, lineno, expr))
    return tuple(dict.fromkeys(names)), tuple(_dedupe_origins(origins))


def _partial_kw_origins(
    call: ast.Call, unit: ModUnit, enclosing: str
) -> list[tuple[str, int, str]]:
    """Keywords bound on ``functools.partial(sdk_fn, key=...)`` when that partial is called."""
    func = call.func
    if not isinstance(func, ast.Name):
        return []
    assigned = unit.assigns.get((enclosing, func.id)) or unit.assigns.get(
        ("<module>", func.id)
    )
    if not isinstance(assigned, ast.Call) or not _looks_like_partial(assigned):
        return []
    lineno = getattr(assigned, "lineno", 0) or 0
    found: list[tuple[str, int, str]] = []
    for keyword in assigned.keywords:
        if keyword.arg:
            found.append(
                (keyword.arg, lineno, f"partial(..., {keyword.arg}=...)")
            )
    return found


def _looks_like_partial(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id == "partial"
    return isinstance(node.func, ast.Attribute) and node.func.attr == "partial"


def _expr(call: ast.Call) -> str:
    try:
        return ast.unparse(call.func)
    except (TypeError, ValueError):
        return "<call>"


def _sym(rel: str, qual: str) -> str:
    return f"{rel}::{qual}"


def _version_gates(
    units: dict[str, ModUnit],
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
) -> list[_Direct]:
    """Lines that branch on an outside service's version."""
    found: list[_Direct] = []
    seen: set[tuple[str, int, str]] = set()
    for rel, unit in units.items():
        scopes: list[tuple[str, ast.AST]] = [("<module>", unit.tree)]
        for qual, defn in unit.defs.items():
            if defn.kind == "class":
                continue
            scopes.append((qual, defn.node))
        for enclosing, scope in scopes:
            aliases = _version_aliases(unit, scope, enclosing, resolver, specs)
            for node in _iter_scope(scope):
                test = _branch_test(node)
                if test is None:
                    continue
                package = _package_in_test(test, unit, enclosing, resolver, specs, aliases)
                if not package:
                    continue
                spec = _spec_named(package, specs)
                if spec is None or spec.family:
                    continue
                expr = _unparse(test)
                key = (rel, getattr(node, "lineno", 0) or 0, expr)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    _Direct(
                        rel=rel,
                        enclosing=enclosing,
                        lineno=key[1],
                        expr=expr,
                        service=spec.service_id,
                        package=spec.package,
                        sdk_method=spec.package,
                        kind="version_gate",
                        params=(),
                        response_fields=(),
                        returns_raw=False,
                        confidence="high",
                    )
                )
    return found


def _branch_test(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.If, ast.While, ast.Assert, ast.IfExp)):
        return node.test
    return None


def _version_aliases(
    unit: ModUnit,
    scope: ast.AST,
    enclosing: str,
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in _iter_scope(scope):
        target, value = _assign_target_value(node)
        if not isinstance(target, ast.Name) or value is None:
            continue
        package = _version_ref(value, unit, enclosing, resolver, specs, {})
        if package:
            aliases[target.id] = package
    return aliases


def _package_in_test(
    test: ast.AST,
    unit: ModUnit,
    enclosing: str,
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
    aliases: dict[str, str],
) -> str:
    for node in ast.walk(test):
        if isinstance(node, ast.Name) and node.id in aliases:
            return aliases[node.id]
        package = _version_ref(node, unit, enclosing, resolver, specs, aliases)
        if package:
            return package
    return ""


def _version_ref(
    node: ast.AST,
    unit: ModUnit,
    enclosing: str,
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
    aliases: dict[str, str],
) -> str:
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if isinstance(node, ast.Attribute) and node.attr == "__version__":
        binding = resolver._resolve_expr(node.value, unit, enclosing, set())
        spec = _service_of(binding, specs)
        if spec is None and binding.target:
            spec = _spec_named(binding.target.split(".")[0], specs)
        if spec is not None and not spec.family:
            return spec.package
        return ""
    if isinstance(node, ast.Call):
        package = _getattr_dunder_version(node, unit, enclosing, resolver, specs)
        if package:
            return package
        return _metadata_version_call(node, unit, enclosing, resolver, specs)
    return ""


def _getattr_dunder_version(
    node: ast.Call,
    unit: ModUnit,
    enclosing: str,
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
) -> str:
    """``getattr(package, "__version__")`` is the same read as ``package.__version__``."""
    name = ""
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name != "getattr" or len(node.args) < 2:
        return ""
    literal = _const_str(node.args[1], unit, enclosing)
    if literal != "__version__":
        return ""
    binding = resolver._resolve_expr(node.args[0], unit, enclosing, set())
    spec = _service_of(binding, specs)
    if spec is None and binding.target:
        spec = _spec_named(binding.target.split(".")[0], specs)
    if spec is None or spec.family:
        return ""
    return spec.package


def _metadata_version_call(
    node: ast.Call,
    unit: ModUnit,
    enclosing: str,
    resolver: _Resolver,
    specs: tuple[ServiceSpec, ...],
) -> str:
    name = ""
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name != "version" or not node.args:
        return ""
    package_name = _const_str(node.args[0], unit, enclosing)
    if not package_name:
        return ""
    spec = _spec_named(package_name, specs)
    if spec is None or spec.family:
        return ""
    binding = resolver._resolve_expr(node.func, unit, enclosing, set())
    if binding.kind == "symbol":
        return ""
    target = binding.target or ""
    if target.endswith("metadata.version") or target.endswith("importlib.metadata.version"):
        return spec.package
    return ""


def _spec_named(name: str, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    token = (name or "").strip()
    if not token:
        return None
    folded = token.lower().replace("_", "-")
    for spec in specs:
        if spec.family:
            continue
        names = {spec.package, spec.service_id, *spec.modules}
        for item in names:
            if item.lower() == token.lower() or item.lower().replace("_", "-") == folded:
                return spec
    return service_for_module(token, specs)


def _propagate(directs: list[_Direct], edges: list[_Edge]) -> list[Chain]:
    reverse: dict[str, list[_Edge]] = {}
    for edge in edges:
        reverse.setdefault(_sym(edge.callee_rel, edge.callee), []).append(edge)
    chains: list[Chain] = []
    for direct in directs:
        origin = _sym(direct.rel, direct.enclosing)
        hop = Hop(direct.rel, direct.lineno, direct.enclosing, direct.expr)
        origins = tuple(
            (name, direct.rel, line, expr) for name, line, expr in direct.param_origins
        )
        initial = Chain(
            service=direct.service,
            package=direct.package,
            call_rel=direct.rel,
            call_lineno=direct.lineno,
            call_symbol=direct.enclosing,
            call_expr=direct.expr,
            hops=(hop,),
            sdk_method=direct.sdk_method,
            kind=direct.kind,
            params=direct.params,
            site_keywords=direct.params,
            response_fields=direct.response_fields,
            confidence=direct.confidence,
            via_wrapper=False,
            param_origins=origins,
        )
        chains.append(initial)
        if (
            direct.kind.endswith("_response")
            or direct.kind.endswith("_ref")
            or direct.kind == "version_gate"
        ):
            continue
        queue: list[tuple[Chain, str, tuple[str, ...], bool]] = [
            (initial, origin, (origin,), direct.returns_raw)
        ]
        while queue:
            chain, current, path, raw = queue.pop()
            for edge in reverse.get(current, []):
                caller = _sym(edge.caller_rel, edge.caller)
                if caller in path or len(chain.hops) >= _MAX_HOPS:
                    continue
                caller_hop = Hop(edge.caller_rel, edge.lineno, edge.caller, edge.expr)
                fields = edge.return_fields if raw else ()
                if raw:
                    chains.extend(_caller_response_chains(direct, chain, edge))
                extended = Chain(
                    service=direct.service,
                    package=direct.package,
                    call_rel=edge.caller_rel,
                    call_lineno=edge.lineno,
                    call_symbol=edge.caller,
                    call_expr=edge.expr,
                    hops=(caller_hop,) + chain.hops,
                    sdk_method=direct.sdk_method,
                    kind=direct.kind,
                    params=direct.params,
                    site_keywords=edge.keywords,
                    response_fields=fields,
                    confidence=min_confidence(chain.confidence, edge.confidence),
                    via_wrapper=True,
                    param_origins=origins,
                )
                chains.append(extended)
                queue.append((extended, caller, path + (caller,), False))
    return chains


def _entries_from_directs(directs: list[_Direct]) -> list[Entry]:
    return [
        Entry(
            service=item.service,
            package=item.package,
            kind=item.kind,
            rel=item.rel,
            lineno=item.lineno,
            symbol=item.sdk_method,
            confidence=item.confidence,
            detail=item.expr,
        )
        for item in directs
    ]


def _dedupe_entries(entries: list[Entry]) -> list[Entry]:
    seen: set[tuple[str, int, str, str]] = set()
    out: list[Entry] = []
    for entry in entries:
        key = (entry.rel, entry.lineno, entry.kind, entry.symbol)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _dedupe_chains(chains: list[Chain]) -> list[Chain]:
    seen: set[tuple[str, int, str, str, str]] = set()
    out: list[Chain] = []
    for chain in chains:
        key = (
            chain.call_rel,
            chain.call_lineno,
            chain.call_symbol,
            chain.sdk_method,
            chain.call_expr,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(chain)
    return out
