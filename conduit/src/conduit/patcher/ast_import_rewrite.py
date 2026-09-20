"""AST import rewrite via libcst (Python) and language engines (JS/TS/Java/Go)."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from conduit.patcher.languages.registry import apply_rewrite_import as _registry_rewrite_import


class _ImportRewriteTransformer(cst.CSTTransformer):
    def __init__(self, old_import: str, new_import: str) -> None:
        self.old_import = old_import
        self.new_import = new_import
        self.changes = 0

    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import:
        new_names = []
        changed = False
        for alias in updated_node.names:
            name = alias.name
            dotted = _dotted_name(name)
            if dotted == self.old_import:
                changed = True
                self.changes += 1
                new_names.append(
                    alias.with_changes(name=_make_attribute(self.new_import))
                )
            else:
                new_names.append(alias)
        if changed:
            return updated_node.with_changes(names=new_names)
        return updated_node

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.BaseSmallStatement | cst.RemovalSentinel:
        if updated_node.module is None:
            return updated_node
        dotted = _dotted_name(updated_node.module)
        # Full module path rewrite only (imported aliases: AST_DECLARATION_REWRITE).
        if dotted == self.old_import:
            self.changes += 1
            return updated_node.with_changes(module=_make_attribute(self.new_import))
        # Prefix rewrite for from old.sub import …
        if dotted.startswith(self.old_import + "."):
            suffix = dotted[len(self.old_import) :]
            self.changes += 1
            return updated_node.with_changes(
                module=_make_attribute(self.new_import + suffix)
            )
        return updated_node


def _dotted_name(node: cst.BaseExpression | cst.Name) -> str:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        parts: list[str] = []
        cur: cst.BaseExpression | None = node
        while isinstance(cur, cst.Attribute):
            parts.append(cur.attr.value)
            cur = cur.value
        if isinstance(cur, cst.Name):
            parts.append(cur.value)
            return ".".join(reversed(parts))
    return ""


def _make_attribute(dotted: str) -> cst.BaseExpression:
    parts = dotted.split(".")
    node: cst.BaseExpression = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def rewrite_python_imports(content: str, old_import: str, new_import: str) -> tuple[str, int]:
    """Module-path rewrite only.

    String fallback is restricted to dotted module tokens with an ``old not in new``
    guard. Bare identifiers and clause fragments never substring-replace.
    """
    import re

    if not old_import or old_import not in content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        updated = content.replace(old_import, new_import)
        return updated, content.count(old_import) if updated != content else 0
    transformer = _ImportRewriteTransformer(old_import, new_import)
    updated = module.visit(transformer)
    if transformer.changes:
        return updated.code, transformer.changes
    # Fallback: dotted module paths only; never bare names / comma fragments.
    _MODULE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
    if (
        _MODULE_TOKEN.fullmatch(old_import)
        and old_import not in new_import
        and old_import in content
    ):
        new_content = content.replace(old_import, new_import)
        return new_content, content.count(old_import)
    return content, 0


def rewrite_js_imports(content: str, old_import: str, new_import: str) -> tuple[str, int]:
    from conduit.patcher.languages.javascript import JsTsEngine

    return JsTsEngine().rewrite_import(content, old_import, new_import)


def apply_import_rewrite(
    path: Path,
    content: str,
    *,
    old_import: str,
    new_import: str,
) -> tuple[str, int]:
    return _registry_rewrite_import(path, content, old_import, new_import)
