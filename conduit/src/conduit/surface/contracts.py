"""Compile packet wire rules into LexicalOnly / Surface contracts."""

from __future__ import annotations

from typing import Any, Mapping

from conduit.surface.types import (
    LexicalOnlyContract,
    PacketContract,
    RenameTerminal,
    ReplaceResolvedExport,
    RewriteIntent,
    Spelling,
    SurfaceContract,
    UseKind,
)


def contracts_from_packet(packet: Mapping[str, Any]) -> tuple[PacketContract, ...]:
    """Compile AST_CALL_REWRITE rules.

    Legacy ``old_callee`` / ``new_callee`` become ``LexicalOnlyContract`` (not
    proof-eligible). Explicit ``surface`` / ``spellings`` fields upgrade to
    ``SurfaceContract``.
    """
    out: list[PacketContract] = []
    for i, rule in enumerate((packet or {}).get("rules") or []):
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "").strip() != "AST_CALL_REWRITE":
            continue
        contract = _contract_from_rule(rule, index=i)
        if contract is not None:
            out.append(contract)
    return tuple(out)


def intent_from(
    *,
    old_export: tuple[str, ...],
    new_callee: str,
) -> RewriteIntent | None:
    """Compile wire callees into a closed rewrite intent.

    Same-prefix leaf renames (``BaseModel.dict`` → ``BaseModel.model_dump``) and
    bare new members become ``RenameTerminal`` so receivers stay intact.
    Owner-changing hops become ``ReplaceResolvedExport``.
    """
    if not old_export:
        return None
    new = (new_callee or "").strip()
    if not new:
        return None
    new_parts = tuple(p for p in new.split(".") if p)
    if not new_parts:
        return None
    old_leaf = old_export[-1]
    if len(new_parts) == 1:
        return RenameTerminal(old_member=old_leaf, new_member=new_parts[0])
    if len(old_export) >= 2 and old_export[:-1] == new_parts[:-1]:
        return RenameTerminal(old_member=old_leaf, new_member=new_parts[-1])
    return ReplaceResolvedExport(source=old_export, target=new_parts)


def _contract_from_rule(rule: dict[str, Any], *, index: int) -> PacketContract | None:
    old = str(rule.get("old_callee") or "").strip()
    new = str(rule.get("new_callee") or "").strip()
    if not old:
        return None

    surface = rule.get("surface") if isinstance(rule.get("surface"), dict) else {}
    export_raw = surface.get("export_path") or rule.get("export_path")
    if isinstance(export_raw, (list, tuple)) and export_raw:
        export_path = tuple(str(p) for p in export_raw if str(p).strip())
    else:
        export_path = tuple(p for p in old.split(".") if p)
    if not export_path:
        return None

    intent = intent_from(old_export=export_path, new_callee=new)
    if intent is None:
        return None

    targets = rule.get("target_files") or ["*"]
    if not isinstance(targets, list):
        targets = ["*"]
    target_files = tuple(str(t) for t in targets)
    sid = str(
        rule.get("surface_id")
        or surface.get("surface_id")
        or f"ast_call:{old}->{new or '?'}:{index}"
    )

    # Upgrade path: explicit surface metadata → proof-eligible SurfaceContract.
    if surface or rule.get("spellings") or rule.get("export_path"):
        spellings = _spellings_from(rule, surface, export_path)
        use_kinds = _use_kinds_from(rule, surface)
        return SurfaceContract(
            surface_id=sid,
            export_path=export_path,
            use_kinds=use_kinds,
            spellings=spellings,
            rewrite=intent,
            target_files=target_files,
            proof_eligible=True,
        )

    return LexicalOnlyContract(
        surface_id=sid,
        old_callee=old,
        new_callee=new,
        export_path=export_path,
        target_files=target_files,
        proof_eligible=False,
    )


def _spellings_from(
    rule: dict[str, Any],
    surface: dict[str, Any],
    export_path: tuple[str, ...],
) -> frozenset[Spelling]:
    raw = surface.get("spellings") or rule.get("spellings")
    if isinstance(raw, (list, tuple, set, frozenset)) and raw:
        out: set[Spelling] = set()
        for item in raw:
            try:
                out.add(Spelling(str(item)))
            except ValueError:
                continue
        if out:
            return frozenset(out)
    base = {Spelling.QUALIFIED, Spelling.IMPORTED}
    if len(export_path) >= 2:
        base.add(Spelling.RECEIVER_MEMBER)
    return frozenset(base)


def _use_kinds_from(
    rule: dict[str, Any], surface: dict[str, Any]
) -> frozenset[UseKind]:
    raw = surface.get("use_kinds") or rule.get("use_kinds")
    if isinstance(raw, (list, tuple, set, frozenset)) and raw:
        out: set[UseKind] = set()
        for item in raw:
            try:
                out.add(UseKind(str(item)))
            except ValueError:
                continue
        if out:
            return frozenset(out)
    return frozenset({UseKind.CALL, UseKind.DECORATOR})
