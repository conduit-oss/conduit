"""AST attribute / call path rewrites (delegates to language engines)."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from conduit.patcher.languages.registry import (
    apply_rename_attr as _registry_rename_attr,
    apply_rewrite_call as _registry_rewrite_call,
)


class _AttrRenameTransformer(cst.CSTTransformer):
    def __init__(self, old_attr: str, new_attr: str) -> None:
        self.old_attr = old_attr
        self.new_attr = new_attr
        self.changes = 0
        self._old_parts = old_attr.split(".")
        self._new_parts = new_attr.split(".")

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        chain = _attr_chain(original_node)
        if chain == self.old_attr:
            self.changes += 1
            return _make_attr(self._new_parts)
        return updated_node


class _CallRewriteTransformer(cst.CSTTransformer):
    def __init__(self, old_callee: str, new_callee: str) -> None:
        self.old_callee = old_callee
        self.new_callee = new_callee
        self.changes = 0
        self._new_parts = new_callee.split(".")

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        name = _attr_chain(original_node.func)
        if name == self.old_callee:
            self.changes += 1
            return updated_node.with_changes(func=_make_attr(self._new_parts))
        return updated_node


def _attr_chain(node: cst.BaseExpression) -> str | None:
    parts: list[str] = []
    cur: cst.BaseExpression | None = node
    while isinstance(cur, cst.Attribute):
        parts.append(cur.attr.value)
        cur = cur.value
    if isinstance(cur, cst.Name):
        parts.append(cur.value)
        return ".".join(reversed(parts))
    return None


def _make_attr(parts: list[str]) -> cst.BaseExpression:
    node: cst.BaseExpression = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def _text_mop_up(content: str, old: str, new: str) -> tuple[str, int]:
    if not old or old in new or old not in content:
        return content, 0
    updated = content.replace(old, new)
    return updated, content.count(old) if updated != content else 0


def rename_python_attr(content: str, old_attr: str, new_attr: str) -> tuple[str, int]:
    if not old_attr or old_attr not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        return _text_mop_up(content, old_attr, new_attr)
    transformer = _AttrRenameTransformer(old_attr, new_attr)
    updated = module.visit(transformer)
    if transformer.changes:
        code, n_extra = _text_mop_up(updated.code, old_attr, new_attr)
        return code, transformer.changes + n_extra
    return _text_mop_up(content, old_attr, new_attr)


def rewrite_python_call(content: str, old_callee: str, new_callee: str) -> tuple[str, int]:
    if not old_callee or old_callee not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        code, count = _text_mop_up(content, old_callee, new_callee)
        return _attach_missing_import(code, count, new_callee)
    transformer = _CallRewriteTransformer(old_callee, new_callee)
    updated = module.visit(transformer)
    if transformer.changes:
        code, n_extra = _text_mop_up(updated.code, old_callee, new_callee)
        return _attach_missing_import(code, transformer.changes + n_extra, new_callee)
    code, count = _text_mop_up(content, old_callee, new_callee)
    return _attach_missing_import(code, count, new_callee)


def _attach_missing_import(code: str, count: int, new_callee: str) -> tuple[str, int]:
    if count <= 0:
        return code, count
    return _ensure_qualified_import(code, new_callee), count


def _qualified_root(callee: str) -> str | None:
    parts = [part for part in callee.split(".") if part]
    if len(parts) < 2:
        return None
    root = parts[0]
    if not root.isidentifier():
        return None
    return root


def _ensure_qualified_import(code: str, new_callee: str) -> str:
    """Import the root of a rewritten qualified callee when it is unbound.

    ``ChatCompletion.create`` rewritten to ``openai.chat.completions.create``
    needs ``import openai`` in a file that only did ``from pkg import
    ChatCompletion`` or ``from llm import complete``. An existing import that
    already binds the root is left alone.
    """
    root = _qualified_root(new_callee)
    if root is None:
        return code
    try:
        module = cst.parse_module(code)
    except Exception:
        return code
    if _import_binds_name(module, root) or not _uses_attribute_root(module, root):
        return code
    return _insert_module_import(module, root).code


def _import_binds_name(module: cst.Module, root: str) -> bool:
    finder = _ImportBinds(root)
    module.visit(finder)
    return finder.found


def _uses_attribute_root(module: cst.Module, root: str) -> bool:
    finder = _AttributeRoot(root)
    module.visit(finder)
    return finder.found


class _ImportBinds(cst.CSTVisitor):
    def __init__(self, root: str) -> None:
        self.root = root
        self.found = False

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            if _alias_binds(alias, self.root):
                self.found = True


class _AttributeRoot(cst.CSTVisitor):
    def __init__(self, root: str) -> None:
        self.root = root
        self.found = False

    def visit_Attribute(self, node: cst.Attribute) -> None:
        cur: cst.BaseExpression = node
        while isinstance(cur, cst.Attribute):
            cur = cur.value
        if isinstance(cur, cst.Name) and cur.value == self.root:
            self.found = True


def _alias_binds(alias: cst.ImportAlias, root: str) -> bool:
    dotted = _attr_chain(alias.name) or ""
    if not dotted:
        return False
    top = dotted.split(".", 1)[0]
    if alias.asname is None:
        return top == root
    local = alias.asname.name
    return isinstance(local, cst.Name) and local.value == root


def _insert_module_import(module: cst.Module, root: str) -> cst.Module:
    body = list(module.body)
    insert_at = 0
    if body and _is_docstring(body[0]):
        insert_at = 1
    while insert_at < len(body) and _is_future_import(body[insert_at]):
        insert_at += 1
    new_stmt = cst.SimpleStatementLine(
        body=[cst.Import(names=[cst.ImportAlias(name=cst.Name(root))])]
    )
    if insert_at < len(body):
        nxt = body[insert_at]
        if nxt.leading_lines:
            new_stmt = new_stmt.with_changes(leading_lines=nxt.leading_lines)
            follow = () if _is_import_stmt(nxt) else (cst.EmptyLine(),)
            body[insert_at] = nxt.with_changes(leading_lines=follow)
        elif not _is_import_stmt(nxt):
            body[insert_at] = nxt.with_changes(leading_lines=(cst.EmptyLine(),))
    body.insert(insert_at, new_stmt)
    return module.with_changes(body=body)


def _is_docstring(stmt: cst.BaseStatement) -> bool:
    if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
        return False
    expr = stmt.body[0]
    return isinstance(expr, cst.Expr) and isinstance(expr.value, cst.SimpleString)


def _is_future_import(stmt: cst.BaseStatement) -> bool:
    if not isinstance(stmt, cst.SimpleStatementLine):
        return False
    for small in stmt.body:
        if (
            isinstance(small, cst.ImportFrom)
            and isinstance(small.module, cst.Name)
            and small.module.value == "__future__"
        ):
            return True
    return False


def _is_import_stmt(stmt: cst.BaseStatement) -> bool:
    if not isinstance(stmt, cst.SimpleStatementLine):
        return False
    return any(isinstance(small, (cst.Import, cst.ImportFrom)) for small in stmt.body)


def apply_attr_rename(
    path: Path, content: str, *, old_attr: str, new_attr: str
) -> tuple[str, int]:
    return _registry_rename_attr(path, content, old_attr, new_attr)


def apply_call_rewrite(
    path: Path, content: str, *, old_callee: str, new_callee: str
) -> tuple[str, int]:
    return _registry_rewrite_call(path, content, old_callee, new_callee)
