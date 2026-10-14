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
    HTTP_CLIENT_ROOTS,
    HTTP_METHOD_TAILS,
    LIMITATIONS,
    ServiceSpec,
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
    #: AWS service from ``client("s3")`` / ``resource("s3")``, when it is a literal.
    cloud_service: str = ""


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
    confidence: str


class _ModuleIndex:
    def __init__(self, rels: list[str]) -> None:
        buckets: dict[str, set[str]] = {}
        for rel in rels:
            dotted = _dotted(rel)
            parts = [part for part in dotted.split(".") if part]
            for index in range(len(parts)):
                key = ".".join(parts[index:])
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
                directs.append(
                    _make_direct(
                        unit,
                        call,
                        spec=spec,
                        sdk_method=method,
                        kind="http_call",
                        confidence=min_confidence(confidence, conf),
                    )
                )
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
            self.unit.assigns[(self.enclosing, name)] = node.value
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self.unit.constants[(self.enclosing, name)] = node.value.value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            if node.annotation is not None:
                self.unit.annotations[(self.enclosing, node.target.id)] = node.annotation
            if node.value is not None:
                self.unit.assigns[(self.enclosing, node.target.id)] = node.value
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    self.unit.constants[(self.enclosing, node.target.id)] = node.value.value
        self.generic_visit(node)

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
            return self._attribute(base, node.attr)
        if isinstance(node, ast.Call):
            func = self._resolve_expr(node.func, unit, enclosing, stack)
            cloud = _factory_service(func, node, unit, enclosing) or func.cloud_service
            return self._call_result(func, cloud_service=cloud)
        return UNKNOWN

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
            if assigned is not None:
                return self._resolve_expr(assigned, unit, enclosing, stack)
            for raw in unit.local_imports.get(enclosing, []):
                if raw.local == name:
                    return self._resolve_import(raw, unit.rel)
            local_def = _local_def(unit, enclosing, name)
            if local_def is not None:
                return Binding("symbol", unit.rel, local_def)
            if enclosing != "<module>" and name not in _params_of(unit, enclosing):
                module_assign = unit.assigns.get(("<module>", name))
                if module_assign is not None:
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

    def _attribute(self, base: Binding, attr: str) -> Binding:
        if base.kind in {"external_module", "external_attr", "instance_external"}:
            return Binding(
                "external_attr",
                f"{base.target}.{attr}",
                confidence=base.confidence,
                cloud_service=base.cloud_service,
            )
        if base.kind == "instance_local":
            method = f"{base.qualname}.{attr}"
            unit = self.units.get(base.target)
            if unit is not None and method in unit.defs:
                return Binding("symbol", base.target, method, confidence=base.confidence)
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

    def _call_result(self, func: Binding, cloud_service: str = "") -> Binding:
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
                )
            unit = self.units.get(func.target)
            defn = unit.defs.get(func.qualname) if unit is not None else None
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
            return Binding(
                "instance_external",
                func.target,
                confidence=func.confidence,
                cloud_service=cloud_service or func.cloud_service,
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
    if root not in {"boto3", "botocore"} and "Session" not in func.target:
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
    url = _call_url(call.node, unit, call.enclosing)
    if not url:
        return None
    host = urlparse(url).hostname or ""
    spec = service_for_host(host, specs)
    if spec is None:
        return None
    verb = parts[-1].upper()
    if verb == "URLOPEN":
        verb = "GET"
    return spec, f"{verb} {url}", "high"


def _call_url(call: ast.Call, unit: ModUnit, enclosing: str) -> str | None:
    for arg in call.args:
        found = _const_str(arg, unit, enclosing)
        if found and found.startswith(("http://", "https://")):
            return found
    for keyword in call.keywords:
        if keyword.arg not in {None, "url"}:
            continue
        found = _const_str(keyword.value, unit, enclosing)
        if found and found.startswith(("http://", "https://")):
            return found
    return None


def _const_str(node: ast.AST, unit: ModUnit, enclosing: str) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return unit.constants.get((enclosing, node.id)) or unit.constants.get(
            ("<module>", node.id)
        )
    return None


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
        params=_keywords(call.node),
        response_fields=fields,
        returns_raw=returns_raw,
        confidence=confidence,
    )


def _response_directs(unit: ModUnit, call: CallRec, direct: _Direct) -> list[_Direct]:
    """Subscript reads of a call's return value, on their own lines."""
    scope = _scope_node(unit, call.enclosing)
    names = _assigned_names(scope, call.node)
    sites = _subscript_sites(scope, names, call.node)
    if not sites:
        return []
    kind = _response_kind(direct.kind)
    found: list[_Direct] = []
    for lineno, expr, path in sites:
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


def _subscript_sites(
    scope: ast.AST, names: tuple[str, ...], call: ast.Call
) -> list[tuple[int, str, str]]:
    wanted = set(names)
    nodes = [node for node in _iter_scope(scope) if isinstance(node, ast.Subscript)]
    inner = {id(node.value) for node in nodes if isinstance(node.value, ast.Subscript)}
    found: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for node in nodes:
        if id(node) in inner:
            continue
        path = ""
        parsed = _use_path(node)
        if parsed is not None:
            root, candidate = parsed
            if root in wanted and "[" in candidate:
                path = candidate
        if not path and _rooted_at_call(node, call):
            path = _call_subscript_path(node, call)
        if "[" not in path:
            continue
        expr = _unparse(node)
        key = (node.lineno, expr)
        if key in seen:
            continue
        seen.add(key)
        found.append((node.lineno or 0, expr, path))
    return found


def _rooted_at_call(node: ast.AST, call: ast.Call) -> bool:
    cur: ast.AST | None = node
    while isinstance(cur, (ast.Subscript, ast.Attribute)):
        cur = cur.value
    return cur is call


def _call_subscript_path(node: ast.Subscript, call: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while cur is not call and isinstance(cur, ast.Subscript):
        parts.append(_slice_text(cur.slice))
        cur = cur.value
    if cur is not call:
        return ""
    parts.reverse()
    return "".join(parts)


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
        _RefScan(unit, resolver, specs, callee_ids, found, seen).visit(unit.tree)
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
    names = _assigned_names(scope, call.node)
    fields = _fields_of_names(scope, names) if names else ()
    return _Edge(
        caller_rel=unit.rel,
        caller=call.enclosing,
        callee_rel=callee_rel,
        callee=callee,
        lineno=call.lineno,
        expr=_expr(call.node),
        keywords=_keywords(call.node),
        return_fields=fields,
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
    if not names:
        return (), raw
    return _fields_of_names(scope, names), raw


def _assigned_names(scope: ast.AST, call: ast.Call) -> tuple[str, ...]:
    names: list[str] = []
    for node in _iter_scope(scope):
        if isinstance(node, ast.Assign) and node.value is call:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is call and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return tuple(dict.fromkeys(names))


def _returns_call(scope: ast.AST, call: ast.Call, names: tuple[str, ...]) -> bool:
    name_set = set(names)
    for node in _iter_scope(scope):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if node.value is call:
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


def _keywords(call: ast.Call) -> tuple[str, ...]:
    names = [kw.arg for kw in call.keywords if kw.arg]
    return tuple(dict.fromkeys(names))


def _expr(call: ast.Call) -> str:
    try:
        return ast.unparse(call.func)
    except (TypeError, ValueError):
        return "<call>"


def _sym(rel: str, qual: str) -> str:
    return f"{rel}::{qual}"


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
        if direct.kind.endswith("_response") or direct.kind.endswith("_ref"):
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
