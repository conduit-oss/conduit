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


def rename_python_attr(content: str, old_attr: str, new_attr: str) -> tuple[str, int]:
    if not old_attr or old_attr not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        updated = content.replace(old_attr, new_attr)
        return updated, content.count(old_attr) if updated != content else 0
    transformer = _AttrRenameTransformer(old_attr, new_attr)
    updated = module.visit(transformer)
    if transformer.changes:
        code = updated.code
        if old_attr in code:
            n_extra = code.count(old_attr)
            code = code.replace(old_attr, new_attr)
            return code, transformer.changes + n_extra
        return code, transformer.changes
    updated = content.replace(old_attr, new_attr)
    return updated, content.count(old_attr) if updated != content else 0


def rewrite_python_call(content: str, old_callee: str, new_callee: str) -> tuple[str, int]:
    if not old_callee or old_callee not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        updated = content.replace(old_callee, new_callee)
        return updated, content.count(old_callee) if updated != content else 0
    transformer = _CallRewriteTransformer(old_callee, new_callee)
    updated = module.visit(transformer)
    if transformer.changes:
        code = updated.code
        if old_callee in code:
            n_extra = code.count(old_callee)
            code = code.replace(old_callee, new_callee)
            return code, transformer.changes + n_extra
        return code, transformer.changes
    updated = content.replace(old_callee, new_callee)
    return updated, content.count(old_callee) if updated != content else 0


def apply_attr_rename(
    path: Path, content: str, *, old_attr: str, new_attr: str
) -> tuple[str, int]:
    return _registry_rename_attr(path, content, old_attr, new_attr)


def apply_call_rewrite(
    path: Path, content: str, *, old_callee: str, new_callee: str
) -> tuple[str, int]:
    return _registry_rewrite_call(path, content, old_callee, new_callee)
