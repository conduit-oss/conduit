"""Incoming third-party webhook handlers.

FastAPI, Flask, and Django routes that receive a Stripe, GitHub, or Twilio
payload. The fields recorded are the ones the handler reads (and a helper
in the same file that the handler passes the payload to).
"""

from __future__ import annotations

import ast

from conduit.outside_in.catalog import ServiceSpec
from conduit.outside_in.graph import ModUnit, _Direct

_ROUTE_TAILS = frozenset(
    {"get", "post", "put", "patch", "delete", "route", "api_route", "add_api_route"}
)
_JSON_TAILS = frozenset({"json", "get_json"})
_BODY_TAILS = frozenset({"body", "data", "get_data"})

# (service id, needles in the path, function name, or string constants)
_PROVIDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stripe", ("stripe", "stripe-signature")),
    ("github", ("github", "x-hub-signature", "x-github-event")),
    ("twilio", ("twilio", "x-twilio-signature")),
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
            provider, confidence = _provider(node)
            if provider is None or provider not in allowed:
                continue
            spec = by_id[provider]
            reads = _field_reads(node, functions)
            if not reads:
                if not _route_paths(node):
                    continue
                reads = ((node.lineno, "", node.name),)
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


def _provider(node: ast.AST) -> tuple[str | None, str]:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    paths = _route_paths(node)
    constants = _string_constants(node)
    blob = " ".join([*paths, node.name, *constants]).lower()
    hooked = any("webhook" in item or item.endswith("/hook") or "/hook" in item for item in (*paths, node.name.lower()))
    header = any(
        needle in blob
        for needle in (
            "stripe-signature",
            "x-hub-signature",
            "x-github-event",
            "x-twilio-signature",
        )
    )
    if not hooked and not header and "webhook" not in node.name.lower():
        return None, "low"
    for service, needles in _PROVIDERS:
        if any(needle in blob for needle in needles):
            confidence = "high" if paths or header else "medium"
            return service, confidence
    return None, "low"


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


def _string_constants(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
    return found


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
