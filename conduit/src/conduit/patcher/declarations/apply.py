"""Declaration rewrite apply / residual scan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import libcst as cst

from conduit.packet.declaration_rules import (
    DeclarationRule,
    ImportMemberSpec,
    InnerClassToAssignmentSpec,
    Symbol,
)
from conduit.patcher.py.imports import ImportLedger, dotted_name, rewrite_import_member


@dataclass(frozen=True)
class StructuralResidual:
    rel: str
    line: int
    kind: str  # import_member | inner_class
    old_shape: str
    reason: str


@dataclass(frozen=True)
class RewriteResult:
    content: str
    applied: int
    residuals: tuple[StructuralResidual, ...]


def _glob_ok(path: Path, patterns: Sequence[str], root: Path) -> bool:
    import fnmatch

    name = path.name
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()
    for pat in patterns:
        if pat == "*" or fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False


def rewrite_declarations(
    path: Path,
    content: str,
    rules: Sequence[DeclarationRule],
    *,
    root: Path | None = None,
) -> RewriteResult:
    """Apply all matching declaration rules to one file (one parse when possible)."""
    root = root or path.parent
    matching = [r for r in rules if _glob_ok(path, r.target_files, root)]
    if not matching:
        return RewriteResult(content=content, applied=0, residuals=())

    updated = content
    applied = 0
    residuals: list[StructuralResidual] = []
    rel = path.name
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        pass

    # Import members first (share import table with nested ensure)
    import_ops = [
        r for r in matching if isinstance(r.operation, ImportMemberSpec)
    ]
    nested_ops = [
        r for r in matching if isinstance(r.operation, InnerClassToAssignmentSpec)
    ]

    for rule in import_ops:
        op = rule.operation
        assert isinstance(op, ImportMemberSpec)
        new_content, n = rewrite_import_member(updated, op.source, op.target)
        if n:
            updated = new_content
            applied += n

    if nested_ops:
        new_content, n, nested_residuals = _rewrite_nested_assigns(
            updated, nested_ops, rel=rel
        )
        if n:
            updated = new_content
            applied += n
        residuals.extend(nested_residuals)

    return RewriteResult(
        content=updated, applied=applied, residuals=tuple(residuals)
    )


def scan_declaration_residuals(
    path: Path,
    content: str,
    rules: Sequence[DeclarationRule],
    *,
    root: Path | None = None,
) -> tuple[StructuralResidual, ...]:
    """Read-only: still-old source shapes for declared obligations."""
    root = root or path.parent
    matching = [r for r in rules if _glob_ok(path, r.target_files, root)]
    if not matching:
        return ()

    rel = path.name
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        pass

    try:
        module = cst.parse_module(content)
    except Exception:
        return ()

    hits: list[StructuralResidual] = []
    for rule in matching:
        op = rule.operation
        if isinstance(op, ImportMemberSpec):
            if _has_import_member(module, op.source):
                hits.append(
                    StructuralResidual(
                        rel=rel,
                        line=0,
                        kind="import_member",
                        old_shape=op.source.identity(),
                        reason="import_member still present",
                    )
                )
        elif isinstance(op, InnerClassToAssignmentSpec):
            if _has_inner_class(module, op):
                hits.append(
                    StructuralResidual(
                        rel=rel,
                        line=0,
                        kind="inner_class",
                        old_shape=op.obligation_id(),
                        reason="inner class still present",
                    )
                )
    return tuple(hits)


def _has_import_member(module: cst.Module, symbol: Symbol) -> bool:
    ledger = ImportLedger(module=module)
    return any(b.kind == "from_import" for b in ledger.bindings_of(symbol))


def _base_names(bases: Sequence[cst.BaseExpression]) -> tuple[str, ...]:
    out: list[str] = []
    for b in bases:
        if isinstance(b, cst.Name):
            out.append(b.value)
        elif isinstance(b, cst.Attribute):
            dotted = dotted_name(b)
            if dotted:
                out.append(dotted.split(".")[-1])
                out.append(dotted)
        elif isinstance(b, cst.Arg) and isinstance(b.value, (cst.Name, cst.Attribute)):
            out.extend(_base_names([b.value]))
    return tuple(out)


def _has_inner_class(module: cst.Module, op: InnerClassToAssignmentSpec) -> bool:
    """True when a matching inner class remains (same selector rules as apply)."""

    def _walk(node: cst.ClassDef, parent_bases: tuple[str, ...] | None) -> bool:
        if parent_bases is not None and node.name.value == op.selector.inner_name:
            if not op.selector.parent_bases_any or any(
                b in parent_bases or b.split(".")[-1] in parent_bases
                for b in op.selector.parent_bases_any
            ):
                return True
        bases = _base_names(node.bases)
        for stmt in node.body.body:
            if isinstance(stmt, cst.ClassDef) and _walk(stmt, bases):
                return True
        return False

    for stmt in module.body:
        if isinstance(stmt, cst.ClassDef) and _walk(stmt, None):
            return True
    return False


def _rewrite_nested_assigns(
    content: str,
    rules: Sequence[DeclarationRule],
    *,
    rel: str,
) -> tuple[str, int, list[StructuralResidual]]:
    try:
        module = cst.parse_module(content)
    except Exception:
        return content, 0, []

    residuals: list[StructuralResidual] = []
    changes = 0
    ensure_syms: list[Symbol] = []

    ops = [
        r.operation
        for r in rules
        if isinstance(r.operation, InnerClassToAssignmentSpec)
    ]

    class _Nested(cst.CSTTransformer):
        def leave_ClassDef(
            self, original: cst.ClassDef, updated: cst.ClassDef
        ) -> cst.ClassDef:
            nonlocal changes
            new_body: list[cst.BaseStatement] = []
            body_changed = False
            parent_bases = _base_names(updated.bases)

            for stmt in updated.body.body:
                matched_op: InnerClassToAssignmentSpec | None = None
                if isinstance(stmt, cst.ClassDef):
                    for op in ops:
                        if stmt.name.value != op.selector.inner_name:
                            continue
                        if op.selector.parent_bases_any:
                            wanted = op.selector.parent_bases_any
                            if not any(
                                b in parent_bases or b.split(".")[-1] in parent_bases
                                for b in wanted
                            ):
                                continue
                        matched_op = op
                        break
                if matched_op is None:
                    new_body.append(stmt)
                    continue

                key_map = dict(matched_op.key_renames)
                entries: list[tuple[str, cst.BaseExpression]] = []
                unsupported = False
                unmapped: list[str] = []

                for inner_stmt in stmt.body.body:
                    if isinstance(inner_stmt, cst.SimpleStatementLine):
                        for small in inner_stmt.body:
                            if isinstance(small, cst.Assign) and len(small.targets) == 1:
                                tgt = small.targets[0].target
                                if isinstance(tgt, cst.Name):
                                    if tgt.value in key_map:
                                        entries.append((key_map[tgt.value], small.value))
                                    else:
                                        unmapped.append(tgt.value)
                                        if matched_op.unmapped_assignments == "refuse":
                                            unsupported = True
                                    continue
                            if isinstance(small, cst.Pass):
                                continue
                            # docstring Expr?
                            if isinstance(small, cst.Expr) and isinstance(
                                small.value, cst.SimpleString
                            ):
                                continue
                            unsupported = True
                    elif isinstance(inner_stmt, cst.SimpleStatementLine):
                        unsupported = True
                    else:
                        # skip empty docstring-only suites handled above
                        if isinstance(inner_stmt, cst.Pass):
                            continue
                        unsupported = True

                if unsupported or (
                    unmapped and matched_op.unmapped_assignments == "refuse"
                ):
                    residuals.append(
                        StructuralResidual(
                            rel=rel,
                            line=0,
                            kind="inner_class",
                            old_shape=matched_op.obligation_id(),
                            reason="inner class refused (unsupported or unmapped)",
                        )
                    )
                    new_body.append(stmt)
                    continue

                if not entries and matched_op.unmapped_assignments == "preserve" and unmapped:
                    # preserve entire class
                    new_body.append(stmt)
                    continue

                args = [
                    cst.Arg(
                        keyword=cst.Name(k),
                        value=v,
                        equal=cst.AssignEqual(
                            whitespace_before=cst.SimpleWhitespace(""),
                            whitespace_after=cst.SimpleWhitespace(""),
                        ),
                    )
                    for k, v in entries
                ]
                # Also keep unmapped as kwargs when preserve
                if matched_op.unmapped_assignments == "preserve":
                    for inner_stmt in stmt.body.body:
                        if not isinstance(inner_stmt, cst.SimpleStatementLine):
                            continue
                        for small in inner_stmt.body:
                            if (
                                isinstance(small, cst.Assign)
                                and len(small.targets) == 1
                                and isinstance(small.targets[0].target, cst.Name)
                            ):
                                name = small.targets[0].target.value
                                if name not in key_map:
                                    args.append(
                                        cst.Arg(
                                            keyword=cst.Name(name),
                                            value=small.value,
                                            equal=cst.AssignEqual(
                                                whitespace_before=cst.SimpleWhitespace(
                                                    ""
                                                ),
                                                whitespace_after=cst.SimpleWhitespace(
                                                    ""
                                                ),
                                            ),
                                        )
                                    )

                ctor = matched_op.emit.constructor
                ctor_node: cst.BaseExpression
                if "." in ctor:
                    parts = ctor.split(".")
                    ctor_node = cst.Name(parts[0])
                    for p in parts[1:]:
                        ctor_node = cst.Attribute(value=ctor_node, attr=cst.Name(p))
                else:
                    ctor_node = cst.Name(ctor)

                assign = cst.SimpleStatementLine(
                    body=[
                        cst.Assign(
                            targets=[
                                cst.AssignTarget(target=cst.Name(matched_op.emit.target))
                            ],
                            value=cst.Call(func=ctor_node, args=args),
                        )
                    ]
                )
                new_body.append(assign)
                body_changed = True
                changes += 1
                if matched_op.emit.ensure_import is not None:
                    ensure_syms.append(matched_op.emit.ensure_import)

            if not body_changed:
                return updated
            return updated.with_changes(
                body=updated.body.with_changes(body=new_body)
            )

    new_module = module.visit(_Nested())
    if ensure_syms:
        ledger = ImportLedger(module=new_module)
        for sym in ensure_syms:
            ledger.ensure(sym)
        new_module = ledger.commit()
        changes += ledger.changes

    code = new_module.code
    if code == content:
        return content, 0, residuals
    return code, changes, residuals
