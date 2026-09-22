"""Pure detector for a decorated def's calling convention."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypeAlias

import libcst as cst

from conduit.packet.declaration_rules import (
    ContextParam,
    DecoratedDefConventionSpec,
    NoSuccessor,
    OptionRename,
    Symbol,
)


@dataclass(frozen=True)
class DecoratedDefView:
    """Framework-free view of one decorated def. The classifier's only input."""

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
    """Already in the target convention. Apply writes nothing; scan reports nothing."""


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
    """Single predicate behind apply and residual scan.

    Structural opacity is first-hit. Param and option gaps then accumulate so
    one site never reshapes one half while refusing the other.
    """
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
    raise NotImplementedError(
        "decorated_def_convention apply_plan is a day-one detector stub"
    )


def build_view(
    func: cst.FunctionDef,
    decorator: cst.Decorator,
    positions: Mapping[Any, Any],
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
    # C2: *args / **kwargs / defaults / non-literal kwargs / bare decorator.
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
            new_literal = remap.get(literal)
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


def _node_line(node: cst.CSTNode, positions: Mapping[Any, Any]) -> int:
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

    def _target_names(target: cst.BaseAssignTargetExpression) -> None:
        if isinstance(target, cst.Name):
            rebinds.add(target.value)
        elif isinstance(target, (cst.Tuple, cst.List)):
            for elt in target.elements:
                if isinstance(elt, cst.Element):
                    _target_names(elt.value)  # type: ignore[arg-type]

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
