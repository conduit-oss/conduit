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


DeclarationOperation: TypeAlias = ImportMemberSpec | InnerClassToAssignmentSpec


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
    raise AssertionError("closed operation union")
