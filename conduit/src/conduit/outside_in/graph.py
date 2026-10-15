"""Static Python call graph from an external service back to call sites.

Resolves imports (including aliases and package re-exports), instance
construction, and ``self`` method calls. Propagation walks callers of a
function that touches a service, recording each hop.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from conduit.outside_in.catalog import (
    CATALOG,
    HTTP_CLIENT_ROOTS,
    HTTP_METHOD_TAILS,
    LIMITATIONS,
    ServiceSpec,
    service_for_host,
    service_for_module,
    synthesize_package,
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
    #: AWS service from ``client("s3")`` / ``resource("s3")``, when it is a literal.
    cloud_service: str = ""
    #: ``base_url`` from an HTTP client constructor, when it is a string.
    base_url: str = ""


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
    constants: dict[tuple[str, str], str] = field(default_factory=dict)
    annotations: dict[tuple[str, str], ast.expr] = field(default_factory=dict)
    returns: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: ``self.engine = create_engine(...)`` keyed by ``(class qualname, attr)``.
    instance_assigns: dict[tuple[str, str], list[tuple[ast.expr, str]]] = field(default_factory=dict)
    #: Base expressions of each class, so a subclass can see fields assigned above it.
    class_bases: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: ``kwargs["temperature"] = 0.3`` keyed by ``(function, dict name)``.
    dict_keys: dict[tuple[str, str], list[str]] = field(default_factory=dict)


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
            # component matches a dependency (``app/providers/stripe.py``)
            # is not that dependency: only a ``src`` / ``lib`` component
            # opens the shorter name, and only when that name is unique.
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
    resolver = _Resolver(units, index)
    entries: list[Entry] = []
    directs: list[_Direct] = []
    edges: list[_Edge] = []
    for rel, unit in units.items():
        entries.extend(resolver.import_entries(rel, specs))
        for call in unit.calls:
            resolved = resolver.resolve_call(unit, call)
            if resolved is None:
                continue
            binding, confidence = resolved
            http = _http_service(binding, call, unit, specs)
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
                continue
            spec = _service_of(binding, specs)
            if spec is None:
                symbol = _symbol_target(binding, units)
                if symbol is not None:
                    edges.append(
                        _make_edge(unit, call, symbol[0], symbol[1], confidence)
                    )
                continue
            kind = _sdk_kind(spec, binding)
            method = _cloud_method(spec, binding, call, unit)
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
    return ServiceMap(
        root=str(root),
        entries=tuple(entries),
        chains=tuple(chains),
        limitations=LIMITATIONS,
    )


def _python_files(root: Path) -> list[str]:
    rels: list[str] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        rels.append(rel)
    rels.sort()
    return rels


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
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing = previous

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Lambda):
                self._bind_lambda(name, node.value)
                return
            self.unit.assigns[(self.enclosing, name)] = node.value
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self.unit.constants[(self.enclosing, name)] = node.value.value
        for target in node.targets:
            self._record_instance_assign(target, node.value)
            self._record_dict_key(target)
        self.generic_visit(node)

    def _record_instance_assign(self, target: ast.AST, value: ast.expr) -> None:
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and self.class_name
        ):
            self.unit.instance_assigns.setdefault((self.class_name, target.attr), []).append(
                (value, self.enclosing)
            )

    def _record_dict_key(self, target: ast.AST) -> None:
        if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
            return
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self.unit.dict_keys.setdefault((self.enclosing, target.value.id), []).append(key.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            if node.annotation is not None:
                self.unit.annotations[(self.enclosing, node.target.id)] = node.annotation
            if isinstance(node.value, ast.Lambda):
                self._bind_lambda(node.target.id, node.value)
                return
            if node.value is not None:
                self.unit.assigns[(self.enclosing, node.target.id)] = node.value
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    self.unit.constants[(self.enclosing, node.target.id)] = node.value.value
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
                self.unit.assigns[(self.enclosing, item.optional_vars.id)] = item.context_expr

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
        self.unit.calls.append(CallRec(node, self.enclosing, getattr(node, "lineno", 0) or 0))
        self.generic_visit(node)

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


class _Resolver:
    def __init__(self, units: dict[str, ModUnit], index: _ModuleIndex) -> None:
        self.units = units
        self.index = index
        self._bindings: dict[str, dict[str, Binding]] = {}
        self._binding_stack: set[str] = set()
        self._returns: dict[str, Binding] = {}
        self._infer_returns()

    def import_entries(self, rel: str, specs: tuple[ServiceSpec, ...]) -> list[Entry]:
        unit = self.units[rel]
        bindings = self._bindings_for(rel)
        out: list[Entry] = []
        for raw in unit.imports:
            if raw.imported == "*":
                spec = service_for_module(raw.module, specs) if raw.module else None
                if spec is not None:
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

    def _infer_returns(self) -> None:
        guards = sum(len(unit.defs) for unit in self.units.values()) + 1
        for _ in range(guards):
            changed = False
            for unit in self.units.values():
                for qual, defn in unit.defs.items():
                    if defn.kind == "class":
                        continue
                    key = _sym(unit.rel, qual)
                    if key in self._returns:
                        continue
                    for expr in unit.returns.get(qual, []):
                        binding = self._resolve_expr(expr, unit, qual, set())
                        if binding.kind in {"instance_external", "instance_local"}:
                            self._returns[key] = binding
                            changed = True
                            break
                    if key in self._returns or defn.node is None:
                        continue
                    announced = self._resolve_annotation(getattr(defn.node, "returns", None), unit, qual)
                    announced = _as_instance(announced)
                    if announced.kind in {"instance_external", "instance_local"}:
                        self._returns[key] = announced
                        changed = True
            if not changed:
                break

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
                    continue
                result[raw.local] = self._resolve_import(raw, rel)
        self._binding_stack.discard(rel)
        self._bindings[rel] = result
        return result

    def _resolve_import(self, raw: RawImport, rel: str) -> Binding:
        dotted = _absolute_module(rel, raw.level, raw.module)
        if raw.imported is None:
            found = self.index.lookup(dotted) if dotted else None
            if found:
                return Binding("module_local", found)
            if dotted:
                return Binding("external_module", dotted)
            return UNKNOWN
        if raw.imported == "*":
            return UNKNOWN
        if not dotted:
            return UNKNOWN
        found = self.index.lookup(dotted)
        if found is None:
            return Binding("external_attr", f"{dotted}.{raw.imported}")
        binds = self._bindings_for(found)
        if raw.imported in binds:
            return binds[raw.imported]
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
    ) -> Binding:
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id, unit, enclosing, stack)
        if isinstance(node, ast.Attribute):
            base = self._resolve_expr(node.value, unit, enclosing, stack)
            return self._attribute(base, node.attr, stack)
        if isinstance(node, ast.Call):
            aliased = self._alias_call(node, unit, enclosing, stack)
            if aliased is not None:
                return aliased
            func = self._resolve_expr(node.func, unit, enclosing, stack)
            cloud = _factory_service(func, node, unit, enclosing) or func.cloud_service
            result = self._call_result(func, node, unit, enclosing, cloud_service=cloud)
            if result.kind == "unknown":
                default = self._mapping_get_default(node, unit, enclosing, stack)
                if default is not None:
                    return default
            return result
        if isinstance(node, ast.Subscript):
            base = self._resolve_expr(node.value, unit, enclosing, stack)
            if base.kind in {"instance_external", "external_attr"} and base.target:
                return Binding(
                    "instance_external",
                    f"{base.target}.__getitem__",
                    confidence=min_confidence(base.confidence, "medium"),
                    cloud_service=base.cloud_service,
                    base_url=base.base_url,
                )
            return UNKNOWN
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value:
            return self._resolve_dotted_text(node.value, unit, enclosing, stack)
        return UNKNOWN

    def _resolve_annotation(
        self,
        node: ast.AST | None,
        unit: ModUnit,
        enclosing: str,
    ) -> Binding:
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self._resolve_dotted_text(node.value, unit, enclosing, set())
        if isinstance(node, ast.Subscript):
            return self._resolve_annotation(node.slice, unit, enclosing)
        return self._resolve_expr(node, unit, enclosing, set())

    def _resolve_dotted_text(
        self,
        text: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
    ) -> Binding:
        parts = [part for part in text.split(".") if part]
        if not parts:
            return UNKNOWN
        binding = self._resolve_name(parts[0], unit, enclosing, stack)
        for attr in parts[1:]:
            if binding.kind == "unknown":
                return UNKNOWN
            binding = self._attribute(binding, attr)
        return binding

    def _mapping_get_default(
        self,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
    ) -> Binding | None:
        """``kwargs.get(key, Factory())`` may be the default value.

        Only when the receiver is not itself an SDK object, so ``client.get``
        stays a call.
        """
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "get" or len(node.args) < 2:
            return None
        receiver = self._resolve_expr(node.func.value, unit, enclosing, stack)
        if receiver.kind in {"external_attr", "external_module", "instance_external"}:
            return None
        default = self._resolve_expr(node.args[1], unit, enclosing, stack)
        if default.kind == "unknown":
            return None
        return default

    def _alias_call(
        self,
        node: ast.Call,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
    ) -> Binding | None:
        """``functools.partial`` and literal ``getattr`` keep the underlying callable."""
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if not func_name or not node.args:
            return None
        func_binding = self._resolve_expr(node.func, unit, enclosing, stack)
        if func_binding.kind == "symbol":
            return None
        if func_name == "partial" and _is_partial(func_binding):
            return self._resolve_expr(node.args[0], unit, enclosing, stack)
        if func_name == "getattr" and len(node.args) >= 2:
            literal = _const_str(node.args[1], unit, enclosing)
            if not literal or not literal.isidentifier():
                return None
            obj = self._resolve_expr(node.args[0], unit, enclosing, stack)
            if obj.kind == "unknown":
                return None
            return self._attribute(obj, literal)
        return None

    def _resolve_name(
        self,
        name: str,
        unit: ModUnit,
        enclosing: str,
        stack: set[tuple[str, str, str]],
    ) -> Binding:
        token = (unit.rel, enclosing, name)
        if token in stack:
            return UNKNOWN
        stack.add(token)
        try:
            defn = unit.defs.get(enclosing)
            if name in {"self", "cls"} and defn is not None and defn.class_name:
                return Binding("instance_local", unit.rel, defn.class_name)
            assigned = unit.assigns.get((enclosing, name))
            if assigned is not None and not _is_none(assigned):
                return self._resolve_expr(assigned, unit, enclosing, stack)
            if name not in _params_of(unit, enclosing):
                for scope in _enclosing_chain(enclosing):
                    for raw in unit.local_imports.get(scope, []):
                        if raw.local == name:
                            return self._resolve_import(raw, unit.rel)
            local_def = _local_def(unit, enclosing, name)
            if local_def is not None:
                return Binding("symbol", unit.rel, local_def)
            if enclosing != "<module>" and name not in _params_of(unit, enclosing):
                module_assign = unit.assigns.get(("<module>", name))
                if module_assign is not None and not _is_none(module_assign):
                    return self._resolve_expr(module_assign, unit, "<module>", stack)
            binding = self._bindings_for(unit.rel).get(name)
            if binding is not None:
                return binding
            annotation = unit.annotations.get((enclosing, name)) or unit.annotations.get(
                ("<module>", name)
            )
            if annotation is not None:
                typed = self._resolve_expr(annotation, unit, enclosing, stack)
                instanced = _as_instance(typed)
                if instanced.kind != "unknown":
                    return Binding(
                        instanced.kind,
                        instanced.target,
                        instanced.qualname,
                        confidence=min_confidence(instanced.confidence, "medium"),
                        cloud_service=instanced.cloud_service,
                    )
            return UNKNOWN
        finally:
            stack.discard(token)

    def _attribute(
        self,
        base: Binding,
        attr: str,
        stack: set[tuple[str, str, str]] | None = None,
    ) -> Binding:
        if base.kind in {"external_module", "external_attr", "instance_external"}:
            return Binding(
                "external_attr",
                f"{base.target}.{attr}",
                confidence=base.confidence,
                cloud_service=base.cloud_service,
                base_url=base.base_url,
            )
        if base.kind == "instance_local":
            method = f"{base.qualname}.{attr}"
            unit = self.units.get(base.target)
            if unit is not None and method in unit.defs:
                return Binding("symbol", base.target, method, confidence=base.confidence)
            found = self._instance_field(base, attr, stack if stack is not None else set())
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
            return UNKNOWN
        if base.kind == "module_local":
            binds = self._bindings_for(base.target)
            if attr in binds:
                return binds[attr]
            parent = _dotted(base.target)
            sub = self.index.lookup(f"{parent}.{attr}") if parent else None
            if sub:
                return Binding("module_local", sub, confidence=base.confidence)
            return UNKNOWN
        return UNKNOWN

    def _instance_field(
        self,
        base: Binding,
        attr: str,
        stack: set[tuple[str, str, str]],
    ) -> Binding | None:
        """A field assigned on this class or one of its bases."""
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
            token = ("field", qual, attr)
            if token not in stack:
                stack.add(token)
                try:
                    for expr, scope in unit.instance_assigns.get((qual, attr), []):
                        resolved = self._resolve_expr(expr, unit, scope, stack)
                        if resolved.kind == "external_attr":
                            resolved = _as_instance(resolved)
                        if resolved.kind != "unknown":
                            return resolved
                finally:
                    stack.discard(token)
            for base_expr in unit.class_bases.get(qual, []):
                while isinstance(base_expr, ast.Subscript):
                    base_expr = base_expr.value
                parent = self._resolve_expr(base_expr, unit, qual, stack)
                if parent.kind == "symbol" and parent.qualname:
                    queue.append((parent.target, parent.qualname))
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
        if func.kind in {"external_attr", "external_module"}:
            base_url = _base_url_from_call(node, unit, enclosing) or func.base_url
            return Binding(
                "instance_external",
                func.target,
                confidence=func.confidence,
                cloud_service=cloud_service or func.cloud_service,
                base_url=base_url,
            )
        return UNKNOWN


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


def _is_none(node: ast.AST) -> bool:
    """``x = None`` in an except clause is the missing-import fallback, not a binding."""
    return isinstance(node, ast.Constant) and node.value is None


def _as_instance(binding: Binding) -> Binding:
    if binding.kind == "external_attr":
        return Binding(
            "instance_external",
            binding.target,
            confidence=binding.confidence,
            cloud_service=binding.cloud_service,
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


def _service_of(binding: Binding, specs: tuple[ServiceSpec, ...]) -> ServiceSpec | None:
    if binding.kind not in {"external_attr", "external_module", "instance_external"}:
        return None
    return service_for_module(binding.target, specs)


def _kind_prefix(spec: ServiceSpec) -> str:
    if spec.kind == "database":
        return "db"
    if spec.kind == "cloud":
        return "cloud"
    return "sdk"


_FACTORY_TAILS = frozenset({"client", "resource"})


def _cloud_factory_roots() -> frozenset[str]:
    """Import roots of catalogued cloud SDKs (``boto3.client``, and the same shape)."""
    roots: set[str] = set()
    for spec in CATALOG.values():
        if spec.kind != "cloud":
            continue
        roots.update(spec.modules)
        roots.update(prefix.split(".")[0] for prefix in spec.module_prefixes if prefix)
    return frozenset(roots)


def _factory_service(
    func: Binding,
    node: ast.Call,
    unit: ModUnit,
    enclosing: str,
) -> str:
    """Literal AWS service passed to ``boto3.client`` / ``resource``."""
    if func.kind not in {"external_attr", "external_module", "instance_external"}:
        return ""
    parts = func.target.split(".")
    if not parts or parts[-1] not in _FACTORY_TAILS:
        return ""
    root = parts[0]
    if root not in _cloud_factory_roots() and "Session" not in func.target:
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


def _cloud_method(spec: ServiceSpec, binding: Binding, call: CallRec, unit: ModUnit) -> str:
    cloud = binding.cloud_service or _factory_service(binding, call.node, unit, call.enclosing)
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


def _http_service(
    binding: Binding,
    call: CallRec,
    unit: ModUnit,
    specs: tuple[ServiceSpec, ...],
) -> tuple[ServiceSpec, str, str] | None:
    if binding.kind != "external_attr":
        return None
    parts = binding.target.split(".")
    if not parts or parts[0] not in HTTP_CLIENT_ROOTS or parts[-1] not in HTTP_METHOD_TAILS:
        return None
    url = _call_url(call.node, unit, call.enclosing, binding.base_url)
    if not url:
        return None
    host = urlparse(url).hostname or ""
    spec = service_for_host(host, specs)
    if spec is None:
        return None
    verb = parts[-1].upper()
    if verb == "REQUEST":
        verb = _request_verb(call.node) or "GET"
    if verb == "URLOPEN":
        verb = "GET"
    return spec, f"{verb} {url}", "high"


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


def _dict_keys(node: ast.AST, unit: ModUnit, enclosing: str, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(node, ast.Dict):
        found: list[str] = []
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append(key.value)
        return found
    if isinstance(node, ast.Name):
        assigned = unit.assigns.get((enclosing, node.id)) or unit.assigns.get(("<module>", node.id))
        if assigned is not None:
            return _dict_keys(assigned, unit, enclosing, depth + 1)
    return []


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
    return _Direct(
        rel=unit.rel,
        enclosing=call.enclosing,
        lineno=call.lineno,
        expr=_expr(call.node),
        service=spec.service_id,
        package=spec.package,
        sdk_method=sdk_method,
        kind=kind,
        params=_keywords(call.node, unit, call.enclosing),
        response_fields=fields,
        returns_raw=returns_raw,
        confidence=confidence,
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


def _assigned_names(scope: ast.AST, call: ast.Call) -> tuple[str, ...]:
    names: list[str] = []
    for node in _iter_scope(scope):
        if isinstance(node, ast.Assign) and _unwrap_await(node.value) is call:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and _unwrap_await(node.value) is call and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _unwrap_await(item.context_expr) is call and isinstance(item.optional_vars, ast.Name):
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
    names = [kw.arg for kw in call.keywords if kw.arg]
    if unit is not None:
        for kw in call.keywords:
            if kw.arg is not None or not isinstance(kw.value, ast.Name):
                continue
            names.extend(_dict_keys(kw.value, unit, enclosing))
            names.extend(unit.dict_keys.get((enclosing, kw.value.id), []))
    return tuple(dict.fromkeys(names))


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
    """Lines that branch on a catalogued library's version."""
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
    synthesized = synthesize_package(token)
    if synthesized is not None:
        return synthesized
    return service_for_module(token, specs)


def _propagate(directs: list[_Direct], edges: list[_Edge]) -> list[Chain]:
    reverse: dict[str, list[_Edge]] = {}
    for edge in edges:
        reverse.setdefault(_sym(edge.callee_rel, edge.callee), []).append(edge)
    chains: list[Chain] = []
    for direct in directs:
        origin = _sym(direct.rel, direct.enclosing)
        hop = Hop(direct.rel, direct.lineno, direct.enclosing, direct.expr)
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
