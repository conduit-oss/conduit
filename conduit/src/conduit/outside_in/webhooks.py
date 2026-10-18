"""Incoming webhook handlers.

A route is a webhook only when the handler receives a payload from a
classified outside service: it reads that payload's event type, or it
verifies a signature header. The service id comes from the route path, the
handler name, or that signature header. A docstring, an import, or a
response header is not evidence.
"""

from __future__ import annotations

import ast
import re

from conduit.outside_in.specs import ServiceSpec, normalize_dist
from conduit.outside_in.graph import ModUnit, _Direct

_ROUTE_TAILS = frozenset(
    {"get", "post", "put", "patch", "delete", "route", "api_route", "add_api_route"}
)
_JSON_TAILS = frozenset({"json", "get_json"})
_BODY_TAILS = frozenset({"body", "data", "get_data"})
_EVENT_KEYS = frozenset({"type", "event", "event_type", "eventtype"})

_STOP = frozenset(
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


def webhook_directs(
    units: dict[str, ModUnit],
    specs: tuple[ServiceSpec, ...],
) -> list[_Direct]:
    allowed = {spec.service_id for spec in specs}
    by_id = {spec.service_id: spec for spec in specs}
    found: list[_Direct] = []
    for rel, unit in units.items():
        functions = _functions(unit.tree)
        for node in functions.values():
            reads = _field_reads(node, functions)
            signatures = _signature_headers(node)
            if not _receives_payload(node, reads, signatures):
                continue
            provider, confidence = _provider(node, specs, signatures)
            if provider is None or provider not in allowed:
                continue
            spec = by_id[provider]
            if not reads:
                header = signatures[0]
                reads = ((node.lineno, header, node.name),)
            for lineno, path, expr in reads:
                found.append(
                    _Direct(
                        rel=rel,
                        enclosing=node.name,
                        lineno=lineno,
                        expr=expr,
                        service=spec.service_id,
                        package=spec.package,
                        sdk_method="webhook",
                        kind="webhook",
                        params=(path,) if path else (),
                        response_fields=(path,) if path else (),
                        returns_raw=False,
                        confidence=confidence,
                    )
                )
    return found


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._fn(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._fn(node)

        def _fn(self, node: ast.AST) -> None:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            qual = ".".join([*self.stack, node.name])
            found[qual] = node
            found.setdefault(node.name, node)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    Visitor().visit(tree)
    return found


def _receives_payload(
    node: ast.AST,
    reads: list[tuple[int, str, str]],
    signatures: list[str],
) -> bool:
    """True when the handler takes an outside payload, not merely serves a route."""
    if signatures:
        return True
    if _event_type_read(reads):
        return True
    return bool(reads) and _webhook_shaped(node)


def _webhook_shaped(node: ast.AST) -> bool:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    names = [*_route_paths(node), node.name.lower()]
    return any("webhook" in item or "/hook" in item or item.endswith("/hook") for item in names)


def _event_type_read(reads: list[tuple[int, str, str]]) -> bool:
    for _lineno, path, _expr in reads:
        leaf = path.rsplit(".", 1)[-1].replace("-", "_").lower()
        if leaf in _EVENT_KEYS:
            return True
    return False


def _signature_headers(node: ast.AST) -> list[str]:
    """Incoming signature headers the handler reads. Response headers do not count."""
    found: list[str] = []
    for child in ast.walk(node):
        key = ""
        if isinstance(child, ast.Subscript) and isinstance(getattr(child, "ctx", None), ast.Load):
            if _is_headers(child.value):
                key = _const_str(child.slice)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and _is_headers(child.func.value)
            and child.args
        ):
            key = _const_str(child.args[0])
        if key and "signature" in key.lower() and key not in found:
            found.append(key)
    return found


def _is_headers(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "headers":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "headers"


def _const_str(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _provider(
    node: ast.AST,
    specs: tuple[ServiceSpec, ...],
    signatures: list[str],
) -> tuple[str | None, str]:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    paths = _route_paths(node)
    blob = " ".join([*paths, node.name, *signatures]).lower()
    tokens = _tokens(blob)
    if not tokens:
        return None, "low"
    for spec in specs:
        if spec.transport or spec.family:
            continue
        if tokens & _spec_tokens(spec):
            confidence = "high" if paths or signatures else "medium"
            return spec.service_id, confidence
    return None, "low"


def _tokens(blob: str) -> set[str]:
    found: set[str] = set()
    for part in re.split(r"[^a-z0-9]+", blob.lower()):
        if len(part) < 3 or part in _STOP:
            continue
        found.add(part)
    return found


def _spec_tokens(spec: ServiceSpec) -> set[str]:
    raw = [spec.service_id, spec.package, *spec.modules]
    found: set[str] = set()
    for item in raw:
        token = normalize_dist(item).replace("-", "")
        if len(token) >= 3 and token not in _STOP:
            found.add(token)
        head = normalize_dist(item.split(".", 1)[0]).replace("-", "")
        if len(head) >= 3 and head not in _STOP:
            found.add(head)
    return found


def _route_paths(node: ast.AST) -> list[str]:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    paths: list[str] = []
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        if call is None or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr not in _ROUTE_TAILS:
            continue
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            paths.append(call.args[0].value)
    return paths


def _field_reads(
    handler: ast.AST,
    functions: dict[str, ast.AST],
) -> list[tuple[int, str, str]]:
    assert isinstance(handler, (ast.FunctionDef, ast.AsyncFunctionDef))
    reads: list[tuple[int, str, str]] = []
    roots: dict[str, str] = {}
    _scan_block(list(handler.body), roots, reads, functions, depth=0)
    return _maximal_reads(reads)


def _scan_block(
    stmts: list[ast.stmt],
    roots: dict[str, str],
    reads: list[tuple[int, str, str]],
    functions: dict[str, ast.AST],
    *,
    depth: int,
) -> None:
    for stmt in stmts:
        _record_expr_reads(stmt, roots, reads)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            _bind_payload(stmt, roots)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            _follow_helper(stmt.value, roots, reads, functions, depth)
        elif isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
            _follow_helper(stmt.value, roots, reads, functions, depth)
        for block in _nested_blocks(stmt):
            _scan_block(block, dict(roots), reads, functions, depth=depth)


def _nested_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for name in ("body", "orelse", "finalbody"):
        value = getattr(stmt, name, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            if stmt is not value and not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                blocks.append(value)
    if isinstance(stmt, ast.Try):
        for handler in stmt.handlers:
            blocks.append(list(handler.body))
    return blocks


def _bind_payload(stmt: ast.AST, roots: dict[str, str]) -> None:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        prefix = _payload_prefix(stmt.value, roots)
        if prefix is not None:
            roots[stmt.targets[0].id] = prefix
        return
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        prefix = _payload_prefix(stmt.value, roots)
        if prefix is not None:
            roots[stmt.target.id] = prefix


def _payload_prefix(expr: ast.AST, roots: dict[str, str]) -> str | None:
    inner = expr.value if isinstance(expr, ast.Await) else expr
    if isinstance(inner, ast.Call):
        func = inner.func
        if isinstance(func, ast.Attribute) and func.attr in _JSON_TAILS:
            return ""
        if _is_json_loads(inner):
            return ""
    if isinstance(inner, ast.Attribute) and inner.attr == "json":
        return ""
    parsed = _subscript_path(inner, roots)
    if parsed:
        return parsed
    if isinstance(inner, ast.Name) and inner.id in roots:
        return roots[inner.id]
    return None


def _is_json_loads(call: ast.Call) -> bool:
    func = call.func
    name = ""
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    if name != "loads" or not call.args:
        return False
    return _is_body(call.args[0])


def _is_body(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _BODY_TAILS:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _BODY_TAILS:
        return True
    return False


def _iter_exprs(stmt: ast.stmt):
    """Walk ``stmt`` but do not enter nested functions or classes."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        yield node
        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _record_expr_reads(
    stmt: ast.stmt,
    roots: dict[str, str],
    reads: list[tuple[int, str, str]],
) -> None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    for node in _iter_exprs(stmt):
        if isinstance(node, ast.Subscript):
            path = _subscript_path(node, roots)
            if path:
                reads.append((getattr(node, "lineno", 0) or 0, path, _unparse(node)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            base = _prefix_of(node.func.value, roots)
            if base is None:
                continue
            key = node.args[0].value
            path = f"{base}.{key}" if base else key
            reads.append((getattr(node, "lineno", 0) or 0, path, _unparse(node)))


def _subscript_path(node: ast.AST, roots: dict[str, str]) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Subscript):
        key = cur.slice
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return ""
        parts.append(key.value)
        cur = cur.value
    if not isinstance(cur, ast.Name) or cur.id not in roots:
        return ""
    parts.reverse()
    base = roots[cur.id]
    path = base
    for part in parts:
        path = f"{path}.{part}" if path else part
    return path


def _prefix_of(node: ast.AST, roots: dict[str, str]) -> str | None:
    path = _subscript_path(node, roots)
    if path:
        return path
    if isinstance(node, ast.Name) and node.id in roots:
        return roots[node.id]
    return None


def _follow_helper(
    call: ast.Call,
    roots: dict[str, str],
    reads: list[tuple[int, str, str]],
    functions: dict[str, ast.AST],
    depth: int,
) -> None:
    if depth >= 1 or not isinstance(call.func, ast.Name):
        return
    helper = functions.get(call.func.id)
    if not isinstance(helper, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    params = [arg.arg for arg in helper.args.args if arg.arg not in {"self", "cls"}]
    sub: dict[str, str] = {}
    for param, arg in zip(params, call.args):
        if isinstance(arg, ast.Name) and arg.id in roots:
            sub[param] = roots[arg.id]
    if not sub:
        return
    _scan_block(list(helper.body), sub, reads, functions, depth=depth + 1)


def _maximal_reads(reads: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    paths = [item[1] for item in reads if item[1]]
    kept: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for lineno, path, expr in reads:
        if not path:
            continue
        if any(other != path and other.startswith(path + ".") for other in paths):
            continue
        key = (lineno, path)
        if key in seen:
            continue
        seen.add(key)
        kept.append((lineno, path, expr))
    return kept


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (TypeError, ValueError):
        return "<payload>"
