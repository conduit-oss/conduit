"""Rewrite definite surface observations; possible stays report-only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import libcst as cst

from conduit.packet.rule_safety import is_valid_python_callee
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.patcher.string_replace import write_if_changed
from conduit.surface.contracts import contracts_from_packet
from conduit.surface.evaluate import evaluate_packet_binding
from conduit.surface.types import (
    Confidence,
    LexicalOnlyContract,
    PacketContract,
    SurfaceContract,
)


@dataclass(frozen=True)
class _ChainEdit:
    old_chain: str
    new_chain: str
    surface_id: str


def apply_definite_surface_rewrites(
    root: Path,
    packet: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> PatchReport:
    """Apply member-preserving rewrites for ``Confidence.DEFINITE`` sites only."""
    root = root.resolve()
    report = PatchReport()
    contracts = contracts_from_packet(packet)
    if not contracts:
        return report

    by_id = {c.surface_id: c for c in contracts}
    binding = evaluate_packet_binding(root, packet)
    edits_by_path: dict[str, list[_ChainEdit]] = {}
    for obs in binding.observations:
        if obs.confidence != Confidence.DEFINITE:
            continue
        contract = by_id.get(obs.surface_id)
        if contract is None:
            continue
        new_chain = replacement_chain(contract, obs.chain)
        if not new_chain or new_chain == obs.chain:
            continue
        if not is_valid_python_callee(obs.chain) or not is_valid_python_callee(new_chain):
            continue
        edits_by_path.setdefault(obs.span.path, []).append(
            _ChainEdit(
                old_chain=obs.chain,
                new_chain=new_chain,
                surface_id=obs.surface_id,
            )
        )

    packet_id = str(packet.get("packet_id") or "packet")
    for rel, edits in edits_by_path.items():
        path = root / rel
        if not path.is_file():
            continue
        try:
            original = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        # Deduplicate identical chain maps; last writer wins per old_chain.
        mapping = {e.old_chain: e.new_chain for e in edits}
        updated, count = rewrite_chains(original, mapping)
        if not count:
            continue
        if write_if_changed(path, original, updated, dry_run=dry_run):
            detail_bits = ", ".join(
                f"{old!r}->{new!r}" for old, new in sorted(mapping.items())
            )
            report.add(
                ChangeRecord(
                    event_id=packet_id,
                    path=rel.replace("\\", "/"),
                    rule_type="SURFACE_DEFINITE_REWRITE",
                    detail=(
                        f"[surface] definite rewrite {detail_bits} ({count}x)"
                    ),
                )
            )
    return report


def replacement_chain(contract: PacketContract, old_chain: str) -> str | None:
    """Preserve receiver; replace renamed member or whole bare/qualified callee."""
    if isinstance(contract, SurfaceContract):
        if contract.rewrite.member:
            parts = old_chain.split(".")
            if not parts:
                return None
            parts[-1] = contract.rewrite.member
            return ".".join(parts)
        if contract.rewrite.export_path:
            new = ".".join(contract.rewrite.export_path)
            if "." not in old_chain:
                return contract.rewrite.export_path[-1]
            return new
        return None

    if isinstance(contract, LexicalOnlyContract):
        new = (contract.new_callee or "").strip()
        if not new:
            return None
        if "." not in old_chain:
            # Bare import/decorator: validator -> field_validator
            return new.split(".")[-1] if new.count(".") == 0 else new.split(".")[-1]
        if "." not in new:
            # Doc/type surface -> member rename: self.dict -> self.model_dump
            parts = old_chain.split(".")
            parts[-1] = new
            return ".".join(parts)
        # Full callee swap when chains align in length or exact old match
        if old_chain == contract.old_callee or old_chain == ".".join(contract.export_path):
            return new
        # Otherwise member-only from the new leaf
        parts = old_chain.split(".")
        parts[-1] = new.split(".")[-1]
        return ".".join(parts)
    return None


def rewrite_chains(content: str, mapping: Mapping[str, str]) -> tuple[str, int]:
    """Rewrite Call/decorator callees whose attr chain is in ``mapping``."""
    if not mapping or not content:
        return content, 0
    try:
        module = cst.parse_module(content)
    except Exception:
        return content, 0
    transformer = _ChainRewriteTransformer(dict(mapping))
    updated = module.visit(transformer)
    if not transformer.changes:
        return content, 0
    return updated.code, transformer.changes


class _ChainRewriteTransformer(cst.CSTTransformer):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.changes = 0

    def leave_Call(
        self, original_node: cst.Call, updated_node: cst.Call
    ) -> cst.Call:
        name = _attr_chain(original_node.func)
        if name and name in self.mapping:
            self.changes += 1
            return updated_node.with_changes(
                func=_make_attr(self.mapping[name].split("."))
            )
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
