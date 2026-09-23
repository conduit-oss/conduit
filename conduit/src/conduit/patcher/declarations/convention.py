from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, TypeAlias

import libcst as cst
from libcst.metadata import CodeRange

from conduit.packet.declaration_rules import (
    ContextParam,
    DecoratedDefConventionSpec,
    NoSuccessor,
    OptionRename,
    Symbol,
)


@dataclass(frozen=True)
class DecoratedDefView:
    name: str
    line: int
    leading_param: str | None
    value_param: str | None
    extra_params: tuple[str, ...]
    annotated_params: frozenset[str]
    has_star_args: bool
    has_star_kwargs: bool
    has_param_defaults: bool
    is_bare_decorator: bool
    decorator_positional: int
    decorator_kwargs: tuple[tuple[str, str], ...]
    decorator_nonliteral_kwargs: tuple[str, ...]
    body_reads: frozenset[str]
    body_rebinds: frozenset[str]


ResidualKind: TypeAlias = Literal[
    "import_member",
    "inner_class",
    "ensure_classmethod",
    "decorated_def_params",
    "decorated_def_options",
]


@dataclass(frozen=True)
class SiteGap:
    kind: ResidualKind
    old_shape: str
    reason: str
    line: int


@dataclass(frozen=True)
class ReshapePlan:
    drop_params: tuple[str, ...]
    add_param: ContextParam | None
    body_substitutions: tuple[tuple[str, str], ...]
    option_edits: tuple[tuple[str, str, str | None], ...]
    ensure_import: Symbol | None


@dataclass(frozen=True)
class Conforms:
    pass


@dataclass(frozen=True)
class Reshape:
    plan: ReshapePlan


@dataclass(frozen=True)
class Refuse:
    gaps: tuple[SiteGap, ...]

    def __post_init__(self) -> None:
        if not self.gaps:
            raise AssertionError("Refuse requires at least one gap")


SiteVerdict: TypeAlias = Conforms | Reshape | Refuse


def classify_site(
    view: DecoratedDefView, spec: DecoratedDefConventionSpec
) -> SiteVerdict:
    shape = _site_shape(view, spec)
    opaque = _opacity_gaps(view, spec, shape)
    if opaque:
        return Refuse(tuple(opaque))

    if view.leading_param != "cls":
        shown = view.leading_param if view.leading_param is not None else "<missing>"
        return Refuse(
            (
                SiteGap(
                    kind="decorated_def_params",
                    old_shape=shape,
                    reason=f"leading parameter {shown!r} is not 'cls'",
                    line=view.line,
                ),
            )
        )

    param_gaps, absorbable = _param_gaps(view, spec, shape)
    option_gaps, renameable = _option_gaps(view, spec, shape)
    gaps = param_gaps + option_gaps
    if gaps:
        return Refuse(tuple(gaps))
    if absorbable or renameable:
        return Reshape(_reshape_plan(spec, absorbable, renameable))
    return Conforms()


def apply_plan(func: cst.FunctionDef, plan: ReshapePlan) -> cst.FunctionDef:
    updated = func
    if plan.option_edits:
        updated = _apply_option_edits(updated, plan.option_edits)
    if plan.drop_params or plan.add_param is not None or plan.body_substitutions:
        updated = _apply_param_plan(updated, plan)
    return updated


def _parse_literal_source(text: str) -> cst.BaseExpression:
    if text in {"True", "False", "None"}:
        return cst.Name(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return cst.SimpleString(text)
    try:
        return cst.parse_expression(text)
    except Exception as exc:
        raise ValueError(f"cannot parse literal source {text!r}") from exc


def _apply_option_edits(
    func: cst.FunctionDef,
    edits: tuple[tuple[str, str, str | None], ...],
) -> cst.FunctionDef:
    by_old = {old: (new_kw, new_lit) for old, new_kw, new_lit in edits}
    new_decs: list[cst.Decorator] = []
    changed = False
    for dec in func.decorators:
        expr = dec.decorator
        if not isinstance(expr, cst.Call):
            new_decs.append(dec)
            continue
        new_args: list[cst.Arg] = []
        pending_add: list[cst.Arg] = []
        seen_new: set[str] = set()
        for arg in expr.args:
            if arg.keyword is None:
                new_args.append(arg)
                continue
            name = arg.keyword.value
            edit = by_old.get(name)
            if edit is None:
                new_args.append(arg)
                continue
            new_kw, new_lit = edit
            changed = True
            if new_lit == "":
                continue
            if new_kw in seen_new or any(
                a.keyword is not None and a.keyword.value == new_kw for a in new_args
            ):
                continue
            value = (
                arg.value
                if new_lit is None
                else _parse_literal_source(new_lit)
            )
            pending_add.append(
                cst.Arg(value=value, keyword=cst.Name(new_kw), equal=cst.AssignEqual())
            )
            seen_new.add(new_kw)
        new_args.extend(pending_add)
        new_decs.append(dec.with_changes(decorator=expr.with_changes(args=new_args)))
    if not changed:
        return func
    return func.with_changes(decorators=new_decs)


def _apply_param_plan(func: cst.FunctionDef, plan: ReshapePlan) -> cst.FunctionDef:
    raise NotImplementedError(
        "decorated_def_convention param/body reshape is not implemented yet"
    )


def build_view(
    func: cst.FunctionDef,
    decorator: cst.Decorator,
    positions: Mapping[cst.CSTNode, CodeRange],
) -> DecoratedDefView:
    line = _node_line(func, positions)
    params = _param_view(func)
    dec_view = _decorator_view(decorator)
    reads, rebinds = _body_names(func)
    return DecoratedDefView(
        name=func.name.value,
        line=line,
        leading_param=params.leading,
        value_param=params.value,
        extra_params=params.extra,
        annotated_params=params.annotated,
        has_star_args=params.has_star_args,
        has_star_kwargs=params.has_star_kwargs,
        has_param_defaults=params.has_defaults,
        is_bare_decorator=dec_view.is_bare,
        decorator_positional=dec_view.positional,
        decorator_kwargs=dec_view.kwargs,
        decorator_nonliteral_kwargs=dec_view.nonliteral,
        body_reads=reads,
        body_rebinds=rebinds,
    )


@dataclass(frozen=True)
class _ParamView:
    leading: str | None
    value: str | None
    extra: tuple[str, ...]
    annotated: frozenset[str]
    has_star_args: bool
    has_star_kwargs: bool
    has_defaults: bool


@dataclass(frozen=True)
class _DecoratorView:
    is_bare: bool
    positional: int
    kwargs: tuple[tuple[str, str], ...]
    nonliteral: tuple[str, ...]


def _site_shape(view: DecoratedDefView, spec: DecoratedDefConventionSpec) -> str:
    names: list[str] = []
    if view.leading_param is not None:
        names.append(view.leading_param)
    if view.value_param is not None:
        names.append(view.value_param)
    names.extend(view.extra_params)
    if view.has_star_args:
        names.append("*args")
    if view.has_star_kwargs:
        names.append("**kwargs")
    return f"@{spec.decorator} {view.name}({', '.join(names)})"


def _opacity_gaps(
    view: DecoratedDefView, spec: DecoratedDefConventionSpec, shape: str
) -> list[SiteGap]:
    gaps: list[SiteGap] = []
    if view.has_star_args:
        gaps.append(
            SiteGap(
                kind="decorated_def_params",
                old_shape=shape,
                reason="*args is structurally opaque",
                line=view.line,
            )
        )
    if view.has_star_kwargs:
        gaps.append(
            SiteGap(
                kind="decorated_def_params",
                old_shape=shape,
                reason="**kwargs is structurally opaque",
                line=view.line,
            )
        )
    if view.has_param_defaults:
        gaps.append(
            SiteGap(
                kind="decorated_def_params",
                old_shape=shape,
                reason="parameter defaults are structurally opaque",
                line=view.line,
            )
        )
    if view.is_bare_decorator:
        gaps.append(
            SiteGap(
                kind="decorated_def_options",
                old_shape=shape,
                reason="bare decorator is structurally opaque",
                line=view.line,
            )
        )
    if view.decorator_nonliteral_kwargs:
        shown = ", ".join(view.decorator_nonliteral_kwargs)
        gaps.append(
            SiteGap(
                kind="decorated_def_options",
                old_shape=shape,
                reason=f"non-literal decorator kwarg is structurally opaque ({shown})",
                line=view.line,
            )
        )
    return gaps


def _param_gaps(
    view: DecoratedDefView, spec: DecoratedDefConventionSpec, shape: str
) -> tuple[list[SiteGap], tuple[str, ...]]:
    unmappable = dict(spec.unmappable_params)
    absorbs = dict(spec.context.absorbs) if spec.context is not None else {}
    context_name = spec.context.name if spec.context is not None else None
    known = spec.known_params()
    gaps: list[SiteGap] = []
    absorbable: list[str] = []
    for param in view.extra_params:
        if context_name is not None and param == context_name:
            continue
        if param in unmappable:
            gaps.append(
                SiteGap(
                    kind="decorated_def_params",
                    old_shape=shape,
                    reason=unmappable[param],
                    line=view.line,
                )
            )
            continue
        if param in absorbs:
            if param in view.body_rebinds:
                gaps.append(
                    SiteGap(
                        kind="decorated_def_params",
                        old_shape=shape,
                        reason=(
                            f"parameter {param!r} is rebound; "
                            "cannot prove reads refer to the parameter"
                        ),
                        line=view.line,
                    )
                )
            else:
                absorbable.append(param)
            continue
        if param not in known:
            gaps.append(
                SiteGap(
                    kind="decorated_def_params",
                    old_shape=shape,
                    reason=f"parameter {param!r} has no declared successor",
                    line=view.line,
                )
            )
    return gaps, tuple(absorbable)


def _option_gaps(
    view: DecoratedDefView, spec: DecoratedDefConventionSpec, shape: str
) -> tuple[list[SiteGap], tuple[tuple[str, str, str | None], ...]]:
    by_kwarg = {opt.kwarg: opt.mapping for opt in spec.options}
    known = spec.known_options()
    present = dict(view.decorator_kwargs)
    gaps: list[SiteGap] = []
    renameable: list[tuple[str, str, str | None]] = []
    for kwarg, literal in view.decorator_kwargs:
        mapping = by_kwarg.get(kwarg)
        if mapping is None:
            if kwarg not in known:
                gaps.append(
                    SiteGap(
                        kind="decorated_def_options",
                        old_shape=f"@{spec.decorator} {kwarg}={literal}",
                        reason=f"decorator kwarg {kwarg!r} has no declared successor",
                        line=view.line,
                    )
                )
            continue
        if isinstance(mapping, NoSuccessor):
            gaps.append(
                SiteGap(
                    kind="decorated_def_options",
                    old_shape=f"@{spec.decorator} {kwarg}={literal}",
                    reason=mapping.reason,
                    line=view.line,
                )
            )
            continue
        if isinstance(mapping, OptionRename):
            remap = dict(mapping.values)
            if remap:
                if literal not in remap:
                    gaps.append(
                        SiteGap(
                            kind="decorated_def_options",
                            old_shape=f"@{spec.decorator} {kwarg}={literal}",
                            reason=(
                                f"decorator kwarg {kwarg!r} literal {literal!r} "
                                "has no declared successor"
                            ),
                            line=view.line,
                        )
                    )
                    continue
                new_literal: str | None = remap[literal]
            else:
                new_literal = literal
            if new_literal != "":
                existing = present.get(mapping.new_kwarg)
                if (
                    existing is not None
                    and existing != new_literal
                    and mapping.new_kwarg != kwarg
                ):
                    gaps.append(
                        SiteGap(
                            kind="decorated_def_options",
                            old_shape=(
                                f"@{spec.decorator} {mapping.new_kwarg}={existing}"
                            ),
                            reason=(
                                f"decorator kwarg conflict: {kwarg!r} would set "
                                f"{mapping.new_kwarg!r}={new_literal} but "
                                f"{mapping.new_kwarg}={existing} is already present"
                            ),
                            line=view.line,
                        )
                    )
                    continue
            renameable.append((kwarg, mapping.new_kwarg, new_literal))
    return gaps, tuple(renameable)


def _reshape_plan(
    spec: DecoratedDefConventionSpec,
    absorbable: tuple[str, ...],
    renameable: tuple[tuple[str, str, str | None], ...],
) -> ReshapePlan:
    substitutions: list[tuple[str, str]] = []
    if spec.context is not None:
        for old in absorbable:
            repl = spec.context.substitution(old)
            if repl is not None:
                substitutions.append((old, repl))
    add_param = spec.context if absorbable else None
    return ReshapePlan(
        drop_params=absorbable,
        add_param=add_param,
        body_substitutions=tuple(substitutions),
        option_edits=renameable,
        ensure_import=spec.context.ensure_import if add_param is not None else None,
    )


def _node_line(
    node: cst.CSTNode, positions: Mapping[cst.CSTNode, CodeRange]
) -> int:
    try:
        rng = positions[node]
        return int(rng.start.line)
    except Exception:
        return 0


def _has_default(param: cst.Param) -> bool:
    return param.default is not None and not isinstance(param.default, cst.MaybeSentinel)


def _param_name(param: cst.Param) -> str | None:
    if param.name is None:
        return None
    return param.name.value


def _param_view(func: cst.FunctionDef) -> _ParamView:
    positional = [*func.params.posonly_params, *func.params.params]
    kwonly = list(func.params.kwonly_params)
    names = [n for n in (_param_name(p) for p in positional) if n]
    leading = names[0] if names else None
    value = names[1] if len(names) > 1 else None
    extra = tuple(names[2:]) + tuple(
        n for n in (_param_name(p) for p in kwonly) if n
    )
    annotated = {
        n
        for p in (*positional, *kwonly)
        for n in (_param_name(p),)
        if n and p.annotation is not None
    }
    star = func.params.star_arg
    has_star_args = isinstance(star, cst.Param) and _param_name(star) is not None
    has_star_kwargs = (
        isinstance(func.params.star_kwarg, cst.Param)
        and _param_name(func.params.star_kwarg) is not None
    )
    defaulted = any(_has_default(p) for p in (*positional, *kwonly))
    if has_star_args and isinstance(star, cst.Param):
        defaulted = defaulted or _has_default(star)
    if has_star_kwargs and isinstance(func.params.star_kwarg, cst.Param):
        defaulted = defaulted or _has_default(func.params.star_kwarg)
    return _ParamView(
        leading=leading,
        value=value,
        extra=extra,
        annotated=frozenset(annotated),
        has_star_args=has_star_args,
        has_star_kwargs=has_star_kwargs,
        has_defaults=defaulted,
    )


def _literal_source(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name) and node.value in {"True", "False", "None"}:
        return node.value
    if isinstance(node, (cst.SimpleString, cst.Integer, cst.Float, cst.Imaginary)):
        return node.value
    if isinstance(node, cst.UnaryOperation) and isinstance(node.operator, cst.Minus):
        inner = _literal_source(node.expression)
        if inner is not None:
            return f"-{inner}"
    return None


def _decorator_view(decorator: cst.Decorator) -> _DecoratorView:
    expr = decorator.decorator
    if not isinstance(expr, cst.Call):
        return _DecoratorView(is_bare=True, positional=0, kwargs=(), nonliteral=())
    positional = 0
    kwargs: list[tuple[str, str]] = []
    nonliteral: list[str] = []
    for arg in expr.args:
        if arg.keyword is None:
            positional += 1
            continue
        name = arg.keyword.value
        literal = _literal_source(arg.value)
        if literal is None:
            nonliteral.append(name)
        else:
            kwargs.append((name, literal))
    return _DecoratorView(
        is_bare=False,
        positional=positional,
        kwargs=tuple(kwargs),
        nonliteral=tuple(nonliteral),
    )


def _body_names(func: cst.FunctionDef) -> tuple[frozenset[str], frozenset[str]]:
    reads: set[str] = set()
    rebinds: set[str] = set()

    def _target_names(target: cst.BaseExpression) -> None:
        if isinstance(target, cst.Name):
            rebinds.add(target.value)
        elif isinstance(target, (cst.Tuple, cst.List)):
            for elt in target.elements:
                if isinstance(elt, cst.Element):
                    _target_names(elt.value)

    class _Walk(cst.CSTVisitor):
        def visit_Name(self, node: cst.Name) -> bool:
            reads.add(node.value)
            return False

        def visit_Assign(self, node: cst.Assign) -> bool:
            for tgt in node.targets:
                _target_names(tgt.target)
            return True

        def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
            _target_names(node.target)
            return True

        def visit_AugAssign(self, node: cst.AugAssign) -> bool:
            _target_names(node.target)
            return True

        def visit_NamedExpr(self, node: cst.NamedExpr) -> bool:
            _target_names(node.target)
            return True

        def visit_For(self, node: cst.For) -> bool:
            _target_names(node.target)
            return True

        def visit_CompFor(self, node: cst.CompFor) -> bool:
            _target_names(node.target)
            return True

        def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
            if node is func:
                return True
            for param in (*node.params.posonly_params, *node.params.params, *node.params.kwonly_params):
                name = _param_name(param)
                if name:
                    rebinds.add(name)
            return False

        def visit_Lambda(self, node: cst.Lambda) -> bool:
            for param in (*node.params.posonly_params, *node.params.params, *node.params.kwonly_params):
                name = _param_name(param)
                if name:
                    rebinds.add(name)
            return False

    func.body.visit(_Walk())
    return frozenset(reads), frozenset(rebinds)
