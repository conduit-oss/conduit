"""Decode AST_DECLARATION_REWRITE wire into immutable domain types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, TypeAlias


class DeclarationRuleError(ValueError):
    """Malformed or contradictory declaration rule."""


@dataclass(frozen=True)
class Symbol:
    module: str
    name: str

    def identity(self) -> str:
        return f"{self.module}::{self.name}"


@dataclass(frozen=True)
class ImportMemberSpec:
    source: Symbol
    target: Symbol
    local_binding: Literal["preserve"] = "preserve"

    def obligation_id(self) -> str:
        return f"import_member:{self.source.identity()}"


@dataclass(frozen=True)
class InnerClassSelector:
    inner_name: str
    parent_bases_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssignmentEmission:
    target: str
    constructor: str
    ensure_import: Symbol | None = None


@dataclass(frozen=True)
class InnerClassToAssignmentSpec:
    selector: InnerClassSelector
    key_renames: tuple[tuple[str, str], ...]
    emit: AssignmentEmission
    unmapped_assignments: Literal["preserve", "refuse"] = "refuse"
    unsupported_members: Literal["refuse"] = "refuse"

    def obligation_id(self) -> str:
        bases = ",".join(self.selector.parent_bases_any) or "*"
        return f"inner_class:{self.selector.inner_name}@{bases}->{self.emit.target}"


@dataclass(frozen=True)
class EnsureClassmethodSpec:
    """Require ``@classmethod`` on defs decorated with ``decorator`` (leaf name)."""

    decorator: str

    def obligation_id(self) -> str:
        return f"ensure_classmethod:{self.decorator}"


@dataclass(frozen=True)
class NoSuccessor:
    """A declared dead end. Presence at a site is a permanent, reasoned gap."""

    reason: str


@dataclass(frozen=True)
class ContextParam:
    """Single v2-style context parameter that absorbs extra old params.

    ``absorbs`` maps an old parameter name to an attribute path on this
    parameter. Body substitution strings are derived from that pair so the
    signature edit cannot disagree with the body edit.
    """

    name: str
    annotation: str | None
    ensure_import: Symbol | None
    absorbs: tuple[tuple[str, str], ...]

    def substitution(self, old_param: str) -> str | None:
        for name, attr in self.absorbs:
            if name == old_param:
                return f"{self.name}.{attr}"
        return None


@dataclass(frozen=True)
class OptionRename:
    """Rename a decorator kwarg, optionally remapping its literal value."""

    new_kwarg: str
    values: tuple[tuple[str, str], ...] = ()


OptionMapping: TypeAlias = OptionRename | NoSuccessor


@dataclass(frozen=True)
class DecoratorOption:
    kwarg: str
    mapping: OptionMapping


@dataclass(frozen=True)
class DecoratedDefConventionSpec:
    """Calling convention of one decorator: kwargs and the def's params.

    Ships with an empty vocabulary as a detector. Mappings are recipe data.
    """

    decorator: str
    context: ContextParam | None = None
    options: tuple[DecoratorOption, ...] = ()
    unmappable_params: tuple[tuple[str, str], ...] = ()
    unknown_params: Literal["refuse"] = "refuse"
    unknown_options: Literal["refuse"] = "refuse"

    def obligation_id(self) -> str:
        leaf = self.decorator.split(".")[-1]
        return f"decorated_def:{leaf}"

    def known_params(self) -> frozenset[str]:
        names = {old for old, _ in (self.context.absorbs if self.context else ())}
        if self.context is not None:
            names.add(self.context.name)
        return frozenset(names)

    def known_options(self) -> frozenset[str]:
        names: set[str] = set()
        for opt in self.options:
            names.add(opt.kwarg)
            if isinstance(opt.mapping, OptionRename):
                names.add(opt.mapping.new_kwarg)
        return frozenset(names)


DeclarationOperation: TypeAlias = (
    ImportMemberSpec
    | InnerClassToAssignmentSpec
    | EnsureClassmethodSpec
    | DecoratedDefConventionSpec
)

DeclStage: TypeAlias = Literal["early", "late"]


def declaration_stage(op: DeclarationOperation) -> DeclStage:
    """Early runs before CALL/ATTR. Late observes the finished decorator stack."""
    match op:
        case ImportMemberSpec():
            return "early"
        case InnerClassToAssignmentSpec():
            return "early"
        case EnsureClassmethodSpec():
            return "late"
        case DecoratedDefConventionSpec():
            return "late"
    raise AssertionError("closed operation union")


@dataclass(frozen=True)
class DeclarationRule:
    target_files: tuple[str, ...]
    operation: DeclarationOperation
    reason: str | None = None
    rule_index: int = -1

    def obligation_id(self) -> str:
        return self.operation.obligation_id()


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeclarationRuleError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_symbol(raw: object, field: str) -> Symbol:
    if not isinstance(raw, Mapping):
        raise DeclarationRuleError(f"{field} must be an object")
    module = _require_str(raw.get("module"), f"{field}.module")
    name = _require_str(raw.get("name"), f"{field}.name")
    if not name.isidentifier():
        raise DeclarationRuleError(f"{field}.name must be an identifier")
    return Symbol(module=module, name=name)


def decode_declaration_rule(
    raw: Mapping[str, Any], *, rule_index: int = -1
) -> DeclarationRule:
    """Wire dict → domain. Only place packet dicts are read for declarations."""
    if str(raw.get("type") or "") != "AST_DECLARATION_REWRITE":
        raise DeclarationRuleError("type must be AST_DECLARATION_REWRITE")

    targets_raw = raw.get("target_files")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise DeclarationRuleError("target_files required")
    target_files = tuple(str(t) for t in targets_raw if str(t).strip())
    if not target_files:
        raise DeclarationRuleError("target_files required")

    op_raw = raw.get("operation")
    if not isinstance(op_raw, Mapping):
        raise DeclarationRuleError("operation must be an object")
    kind = str(op_raw.get("kind") or "").strip()

    if kind == "import_member":
        source = _parse_symbol(op_raw.get("source"), "operation.source")
        target = _parse_symbol(op_raw.get("target"), "operation.target")
        if source == target:
            raise DeclarationRuleError("import_member source and target must differ")
        binding = str(op_raw.get("local_binding") or "preserve").strip()
        if binding != "preserve":
            raise DeclarationRuleError("local_binding must be 'preserve'")
        operation: DeclarationOperation = ImportMemberSpec(
            source=source, target=target, local_binding="preserve"
        )
    elif kind == "inner_class_to_assignment":
        sel_raw = op_raw.get("selector")
        if not isinstance(sel_raw, Mapping):
            raise DeclarationRuleError("operation.selector required")
        inner = _require_str(sel_raw.get("inner_name"), "selector.inner_name")
        if not inner.isidentifier():
            raise DeclarationRuleError("selector.inner_name must be an identifier")
        bases_raw = sel_raw.get("parent_bases_any") or []
        if bases_raw is None:
            bases_raw = []
        if not isinstance(bases_raw, list):
            raise DeclarationRuleError("parent_bases_any must be an array")
        bases = tuple(str(b).strip() for b in bases_raw if str(b).strip())

        keys_raw = op_raw.get("keys")
        if not isinstance(keys_raw, Mapping) or not keys_raw:
            raise DeclarationRuleError("keys map required")
        renames: list[tuple[str, str]] = []
        seen_old: set[str] = set()
        seen_new: set[str] = set()
        for old_k, new_k in keys_raw.items():
            old_s = str(old_k).strip()
            new_s = str(new_k).strip()
            if not old_s.isidentifier() or not new_s.isidentifier():
                raise DeclarationRuleError("keys must map identifiers to identifiers")
            if old_s in seen_old or new_s in seen_new:
                raise DeclarationRuleError("keys must be unique on both sides")
            seen_old.add(old_s)
            seen_new.add(new_s)
            renames.append((old_s, new_s))

        emit_raw = op_raw.get("emit")
        if not isinstance(emit_raw, Mapping):
            raise DeclarationRuleError("emit required")
        target = _require_str(emit_raw.get("target"), "emit.target")
        constructor = _require_str(emit_raw.get("constructor"), "emit.constructor")
        ensure: Symbol | None = None
        if emit_raw.get("ensure_import") is not None:
            ensure = _parse_symbol(emit_raw.get("ensure_import"), "emit.ensure_import")

        unmapped = str(op_raw.get("unmapped_assignments") or "refuse").strip()
        if unmapped not in {"preserve", "refuse"}:
            raise DeclarationRuleError("unmapped_assignments must be preserve|refuse")
        unsupported = str(op_raw.get("unsupported_members") or "refuse").strip()
        if unsupported != "refuse":
            raise DeclarationRuleError("unsupported_members must be refuse")

        operation = InnerClassToAssignmentSpec(
            selector=InnerClassSelector(inner_name=inner, parent_bases_any=bases),
            key_renames=tuple(renames),
            emit=AssignmentEmission(
                target=target, constructor=constructor, ensure_import=ensure
            ),
            unmapped_assignments=unmapped,  # type: ignore[arg-type]
            unsupported_members="refuse",
        )
    elif kind == "ensure_classmethod":
        decorator = _require_str(op_raw.get("decorator"), "operation.decorator")
        leaf = decorator.split(".")[-1]
        if not leaf.isidentifier():
            raise DeclarationRuleError("decorator leaf must be an identifier")
        operation = EnsureClassmethodSpec(decorator=decorator)
    elif kind == "decorated_def_convention":
        operation = _decode_decorated_def_convention(op_raw)
    else:
        raise DeclarationRuleError(f"unknown operation.kind: {kind!r}")

    reason = raw.get("reason")
    reason_s = str(reason).strip() if isinstance(reason, str) and reason.strip() else None
    return DeclarationRule(
        target_files=target_files,
        operation=operation,
        reason=reason_s,
        rule_index=rule_index,
    )


def declaration_rules(packet: Mapping[str, Any] | object) -> tuple[DeclarationRule, ...]:
    """Decode all AST_DECLARATION_REWRITE rules; fail on identity conflicts."""
    rules_raw = getattr(packet, "get", lambda *_: None)("rules") if not isinstance(
        packet, Mapping
    ) else packet.get("rules")
    if not isinstance(rules_raw, list):
        return ()

    out: list[DeclarationRule] = []
    seen: dict[str, DeclarationRule] = {}
    for idx, raw in enumerate(rules_raw):
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("type") or "") != "AST_DECLARATION_REWRITE":
            continue
        rule = decode_declaration_rule(raw, rule_index=idx)
        oid = rule.obligation_id()
        prev = seen.get(oid)
        if prev is not None and prev.operation != rule.operation:
            raise DeclarationRuleError(
                f"conflicting AST_DECLARATION_REWRITE for {oid}"
            )
        if prev is None:
            seen[oid] = rule
            out.append(rule)
    return tuple(out)


def declaration_old_tokens(rule: DeclarationRule) -> tuple[str, ...]:
    """Tokens for source-usage scoping / oracles (not substring-safe for short names)."""
    match rule.operation:
        case ImportMemberSpec(source=source):
            return (source.module, source.name, f"{source.module}.{source.name}")
        case InnerClassToAssignmentSpec(selector=selector, key_renames=renames):
            return (selector.inner_name, *(old for old, _ in renames))
        case EnsureClassmethodSpec(decorator=decorator):
            leaf = decorator.split(".")[-1]
            return (decorator, leaf) if decorator != leaf else (leaf,)
        case DecoratedDefConventionSpec() as op:
            leaf = op.decorator.split(".")[-1]
            names = (op.decorator, leaf) if op.decorator != leaf else (leaf,)
            if op.context is not None:
                names += tuple(old for old, _ in op.context.absorbs)
            names += tuple(opt.kwarg for opt in op.options)
            return names
    raise AssertionError("closed operation union")


def _require_identifier(value: object, field: str) -> str:
    text = _require_str(value, field)
    if not text.isidentifier():
        raise DeclarationRuleError(f"{field} must be an identifier")
    return text


def _decode_refuse_slot(value: object, field: str) -> Literal["refuse"]:
    slot = str(value or "refuse").strip()
    if slot != "refuse":
        raise DeclarationRuleError(f"{field} must be 'refuse'")
    return "refuse"


def _decode_context_param(raw: object) -> ContextParam:
    if not isinstance(raw, Mapping):
        raise DeclarationRuleError("operation.context must be an object")
    name = _require_identifier(raw.get("name"), "context.name")
    annotation_raw = raw.get("annotation")
    annotation: str | None
    if annotation_raw is None or annotation_raw == "":
        annotation = None
    else:
        annotation = _require_str(annotation_raw, "context.annotation")
    ensure: Symbol | None = None
    if raw.get("ensure_import") is not None:
        ensure = _parse_symbol(raw.get("ensure_import"), "context.ensure_import")
    absorbs_raw = raw.get("absorbs") or {}
    if not isinstance(absorbs_raw, Mapping):
        raise DeclarationRuleError("context.absorbs must be an object")
    absorbs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old, attr in absorbs_raw.items():
        old_s = _require_identifier(old, "context.absorbs key")
        attr_s = _require_str(attr, f"context.absorbs.{old_s}")
        if old_s in seen:
            raise DeclarationRuleError("context.absorbs keys must be unique")
        seen.add(old_s)
        absorbs.append((old_s, attr_s))
    return ContextParam(
        name=name,
        annotation=annotation,
        ensure_import=ensure,
        absorbs=tuple(absorbs),
    )


def _decode_decorator_option(raw: object, index: int) -> DecoratorOption:
    if not isinstance(raw, Mapping):
        raise DeclarationRuleError(f"options[{index}] must be an object")
    kwarg = _require_identifier(raw.get("kwarg"), f"options[{index}].kwarg")
    no_successor = raw.get("no_successor")
    rename_to = raw.get("rename_to")
    if no_successor is not None and rename_to is not None:
        raise DeclarationRuleError(
            f"options[{index}] cannot set both rename_to and no_successor"
        )
    if no_successor is not None:
        reason = _require_str(no_successor, f"options[{index}].no_successor")
        return DecoratorOption(kwarg=kwarg, mapping=NoSuccessor(reason=reason))
    if rename_to is None:
        raise DeclarationRuleError(
            f"options[{index}] requires rename_to or no_successor"
        )
    new_kwarg = _require_identifier(rename_to, f"options[{index}].rename_to")
    values_raw = raw.get("values") or {}
    if not isinstance(values_raw, Mapping):
        raise DeclarationRuleError(f"options[{index}].values must be an object")
    values = tuple(
        (str(old), str(new))
        for old, new in values_raw.items()
        if str(old) and str(new)
    )
    return DecoratorOption(
        kwarg=kwarg, mapping=OptionRename(new_kwarg=new_kwarg, values=values)
    )


def _decode_unmappable_params(raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeclarationRuleError("unmappable_params must be an array")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise DeclarationRuleError(f"unmappable_params[{idx}] must be an object")
        param = _require_identifier(item.get("param"), f"unmappable_params[{idx}].param")
        reason = _require_str(item.get("reason"), f"unmappable_params[{idx}].reason")
        if param in seen:
            raise DeclarationRuleError("unmappable_params names must be unique")
        seen.add(param)
        out.append((param, reason))
    return tuple(out)


def _decode_decorated_def_convention(op_raw: Mapping[str, Any]) -> DecoratedDefConventionSpec:
    decorator = _require_str(op_raw.get("decorator"), "operation.decorator")
    leaf = decorator.split(".")[-1]
    if not leaf.isidentifier():
        raise DeclarationRuleError("decorator leaf must be an identifier")
    if op_raw.get("absorbs"):
        raise DeclarationRuleError("absorbs requires context")
    context_raw = op_raw.get("context")
    context: ContextParam | None
    if context_raw is None:
        context = None
    else:
        context = _decode_context_param(context_raw)
    options_raw = op_raw.get("options") or []
    if not isinstance(options_raw, list):
        raise DeclarationRuleError("options must be an array")
    options = tuple(
        _decode_decorator_option(item, idx) for idx, item in enumerate(options_raw)
    )
    return DecoratedDefConventionSpec(
        decorator=decorator,
        context=context,
        options=options,
        unmappable_params=_decode_unmappable_params(op_raw.get("unmappable_params")),
        unknown_params=_decode_refuse_slot(
            op_raw.get("unknown_params"), "unknown_params"
        ),
        unknown_options=_decode_refuse_slot(
            op_raw.get("unknown_options"), "unknown_options"
        ),
    )
