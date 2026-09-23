"""Declaration rewrite apply / residual scan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import libcst as cst
from libcst.metadata import CodeRange, MetadataWrapper, PositionProvider

from conduit.packet.declaration_rules import (
    DeclarationRule,
    DecoratedDefConventionSpec,
    EnsureClassmethodSpec,
    ImportMemberSpec,
    InnerClassToAssignmentSpec,
    Symbol,
)
from conduit.patcher.declarations.convention import (
    Conforms,
    Refuse,
    ResidualKind,
    Reshape,
    SiteGap,
    apply_plan,
    build_view,
    classify_site,
)
from conduit.patcher.py.imports import ImportLedger, dotted_name, rewrite_import_member


@dataclass(frozen=True)
class StructuralResidual:
    rel: str
    line: int
    kind: ResidualKind
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
    classmethod_ops = [
        r for r in matching if isinstance(r.operation, EnsureClassmethodSpec)
    ]
    convention_ops = [
        r for r in matching if isinstance(r.operation, DecoratedDefConventionSpec)
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

    if classmethod_ops:
        new_content, n = _rewrite_ensure_classmethod(updated, classmethod_ops)
        if n:
            updated = new_content
            applied += n

    if convention_ops:
        new_content, n, convention_residuals = _rewrite_convention(
            updated, convention_ops, rel=rel
        )
        if n:
            updated = new_content
            applied += n
        residuals.extend(convention_residuals)

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
        raw = cst.parse_module(content)
    except Exception:
        return ()
    wrapper = MetadataWrapper(raw)
    positions = wrapper.resolve(PositionProvider)
    module = wrapper.module

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
        elif isinstance(op, EnsureClassmethodSpec):
            for line, shape in _missing_classmethod_sites(module, op):
                hits.append(
                    StructuralResidual(
                        rel=rel,
                        line=line,
                        kind="ensure_classmethod",
                        old_shape=shape,
                        reason="decorator present without @classmethod",
                    )
                )
        elif isinstance(op, DecoratedDefConventionSpec):
            hits.extend(_scan_convention(module, positions, op, rel=rel))
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


def _decorator_leaf(expr: cst.BaseExpression) -> str | None:
    if isinstance(expr, cst.Call):
        return _decorator_leaf(expr.func)
    if isinstance(expr, cst.Name):
        return expr.value
    if isinstance(expr, cst.Attribute):
        dotted = dotted_name(expr)
        if dotted:
            return dotted.split(".")[-1]
    return None


def _decorator_matches(expr: cst.BaseExpression, wanted: str) -> bool:
    leaf_wanted = wanted.split(".")[-1]
    if isinstance(expr, cst.Call):
        return _decorator_matches(expr.func, wanted)
    if isinstance(expr, cst.Name):
        return expr.value == leaf_wanted or expr.value == wanted
    if isinstance(expr, cst.Attribute):
        dotted = dotted_name(expr) or ""
        return dotted == wanted or dotted.endswith("." + leaf_wanted) or (
            dotted.split(".")[-1] == leaf_wanted
        )
    return False


def _has_classmethod_decorator(decorators: Sequence[cst.Decorator]) -> bool:
    return any(_decorator_matches(d.decorator, "classmethod") for d in decorators)


def _has_matching_decorator(
    decorators: Sequence[cst.Decorator], wanted: str
) -> bool:
    return any(_decorator_matches(d.decorator, wanted) for d in decorators)


def _missing_classmethod_sites(
    module: cst.Module, op: EnsureClassmethodSpec
) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []

    class _Walk(cst.CSTVisitor):
        def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
            if not _has_matching_decorator(node.decorators, op.decorator):
                return True
            if _has_classmethod_decorator(node.decorators):
                return True
            hits.append((0, f"@{op.decorator} {node.name.value}"))
            return True

    module.visit(_Walk())
    return hits


def _rewrite_ensure_classmethod(
    content: str,
    rules: Sequence[DeclarationRule],
) -> tuple[str, int]:
    ops = [
        r.operation
        for r in rules
        if isinstance(r.operation, EnsureClassmethodSpec)
    ]
    if not ops:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        return content, 0

    changes = 0

    class _Ensure(cst.CSTTransformer):
        def leave_FunctionDef(
            self, original: cst.FunctionDef, updated: cst.FunctionDef
        ) -> cst.FunctionDef:
            nonlocal changes
            if _has_classmethod_decorator(updated.decorators):
                return updated
            matched: EnsureClassmethodSpec | None = None
            for op in ops:
                if _has_matching_decorator(updated.decorators, op.decorator):
                    matched = op
                    break
            if matched is None:
                return updated

            new_decs: list[cst.Decorator] = []
            inserted = False
            for dec in updated.decorators:
                new_decs.append(dec)
                if not inserted and _decorator_matches(dec.decorator, matched.decorator):
                    new_decs.append(
                        cst.Decorator(decorator=cst.Name("classmethod"))
                    )
                    inserted = True
            if not inserted:
                return updated
            changes += 1
            return updated.with_changes(decorators=new_decs)

    new_module = module.visit(_Ensure())
    if changes == 0 or new_module.code == content:
        return content, 0
    return new_module.code, changes


def _gap_to_residual(rel: str, gap: SiteGap) -> StructuralResidual:
    return StructuralResidual(
        rel=rel,
        line=gap.line,
        kind=gap.kind,
        old_shape=gap.old_shape,
        reason=gap.reason,
    )


def _matching_decorator(
    func: cst.FunctionDef, wanted: str
) -> cst.Decorator | None:
    for dec in func.decorators:
        if _decorator_matches(dec.decorator, wanted):
            return dec
    return None


def _iter_function_defs(module: cst.Module) -> list[cst.FunctionDef]:
    found: list[cst.FunctionDef] = []

    class _Walk(cst.CSTVisitor):
        def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
            found.append(node)
            return True

    module.visit(_Walk())
    return found


def _disposition_residuals(
    func: cst.FunctionDef,
    decorator: cst.Decorator,
    spec: DecoratedDefConventionSpec,
    positions: Mapping[cst.CSTNode, CodeRange],
    *,
    rel: str,
) -> list[StructuralResidual]:
    verdict = classify_site(build_view(func, decorator, positions), spec)
    match verdict:
        case Conforms():
            return []
        case Refuse(gaps):
            return [_gap_to_residual(rel, gap) for gap in gaps]
        case Reshape():
            line = 0
            try:
                line = int(positions[func].start.line)
            except Exception:
                line = 0
            return [
                StructuralResidual(
                    rel=rel,
                    line=line,
                    kind="decorated_def_params",
                    old_shape=f"@{spec.decorator} {func.name.value}",
                    reason="declared reshape not yet applied",
                )
            ]


def _rewrite_convention(
    content: str,
    rules: Sequence[DeclarationRule],
    *,
    rel: str,
) -> tuple[str, int, list[StructuralResidual]]:
    specs = [
        r.operation
        for r in rules
        if isinstance(r.operation, DecoratedDefConventionSpec)
    ]
    if not specs:
        return content, 0, []
    try:
        raw = cst.parse_module(content)
    except Exception:
        return content, 0, []
    wrapper = MetadataWrapper(raw)
    positions = wrapper.resolve(PositionProvider)
    module = wrapper.module
    applied = 0
    residuals: list[StructuralResidual] = []

    class _Apply(cst.CSTTransformer):
        def leave_FunctionDef(
            self, original: cst.FunctionDef, updated: cst.FunctionDef
        ) -> cst.FunctionDef:
            nonlocal applied
            current = updated
            for spec in specs:
                dec = _matching_decorator(current, spec.decorator)
                if dec is None:
                    continue
                view = build_view(current, dec, positions)
                verdict = classify_site(view, spec)
                match verdict:
                    case Conforms():
                        continue
                    case Refuse(gaps):
                        residuals.extend(
                            _gap_to_residual(rel, gap) for gap in gaps
                        )
                    case Reshape(plan):
                        if (
                            plan.drop_params
                            or plan.add_param is not None
                            or plan.body_substitutions
                            or plan.ensure_import is not None
                        ):
                            residuals.append(
                                StructuralResidual(
                                    rel=rel,
                                    line=view.line,
                                    kind="decorated_def_params",
                                    old_shape=f"@{spec.decorator} {current.name.value}",
                                    reason="declared reshape not yet applied",
                                )
                            )
                            continue
                        if not plan.option_edits:
                            continue
                        try:
                            rewritten = apply_plan(current, plan)
                        except NotImplementedError:
                            residuals.append(
                                StructuralResidual(
                                    rel=rel,
                                    line=view.line,
                                    kind="decorated_def_params",
                                    old_shape=(
                                        f"@{spec.decorator} {current.name.value}"
                                    ),
                                    reason="declared reshape not yet applied",
                                )
                            )
                            continue
                        if rewritten is not current:
                            applied += 1
                            current = rewritten
            return current

    new_module = module.visit(_Apply())
    if applied == 0:
        return content, 0, residuals
    return new_module.code, applied, residuals


def _scan_convention(
    module: cst.Module,
    positions: Mapping[cst.CSTNode, CodeRange],
    spec: DecoratedDefConventionSpec,
    *,
    rel: str,
) -> list[StructuralResidual]:
    hits: list[StructuralResidual] = []
    for func in _iter_function_defs(module):
        dec = _matching_decorator(func, spec.decorator)
        if dec is None:
            continue
        hits.extend(_disposition_residuals(func, dec, spec, positions, rel=rel))
    return hits
