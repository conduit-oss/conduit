"""Rewrite definite surface observations; possible stays report-only."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import libcst as cst

from conduit.packet.rule_safety import is_valid_python_callee
from conduit.patcher.engine import ChangeRecord, PatchReport
from conduit.patcher.string_replace import write_if_changed
from conduit.surface.contracts import contracts_from_packet, intent_from
from conduit.surface.evaluate import evaluate_packet_binding
from conduit.surface.types import (
    Confidence,
    LexicalOnlyContract,
    MatchEvidence,
    Observation,
    PacketContract,
    RenameTerminal,
    ReplaceResolvedExport,
    Rewrite,
    RewriteIntent,
    SourceSpan,
    Spelling,
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
    """Apply member-preserving rewrites for ``Confidence.DEFINITE`` sites only.

    Honors each contract's ``target_files`` the same way the SDK rule engine does,
    so a misspecified glob cannot be bypassed by the surface path.
    """
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
        if not _path_matches_targets(obs.span.path, contract.target_files):
            continue
        new_chain = materialize(_intent_for(contract), obs)
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


def _path_matches_targets(rel: str, patterns: Sequence[str]) -> bool:
    """True when ``rel`` matches any target_files glob (name or repo-relative)."""
    if not patterns:
        return True
    norm = rel.replace("\\", "/")
    name = Path(norm).name
    for pattern in patterns:
        pat = str(pattern or "").strip()
        if not pat or pat == "*":
            return True
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(norm, pat):
            return True
    return False


def _intent_for(contract: PacketContract) -> RewriteIntent | None:
    if isinstance(contract, SurfaceContract):
        rewrite = contract.rewrite
        if isinstance(rewrite, (RenameTerminal, ReplaceResolvedExport)):
            return rewrite
        if isinstance(rewrite, Rewrite):
            # Legacy ShapeContract.rewrite during migration.
            if rewrite.member:
                return RenameTerminal(
                    old_member=contract.export_path[-1],
                    new_member=rewrite.member,
                )
            if rewrite.export_path:
                return intent_from(
                    old_export=contract.export_path,
                    new_callee=".".join(rewrite.export_path),
                )
        return None
    if isinstance(contract, LexicalOnlyContract):
        return intent_from(
            old_export=contract.export_path or tuple(
                p for p in contract.old_callee.split(".") if p
            ),
            new_callee=contract.new_callee,
        )
    return None


def materialize(intent: RewriteIntent | None, observation: Observation) -> str | None:
    """Build a replacement chain from a closed intent and a typed observation.

    ``RenameTerminal`` always preserves the observed receiver prefix.
    ``ReplaceResolvedExport`` never applies to ``RECEIVER_MEMBER`` sites.
    """
    if intent is None:
        return None
    chain = observation.chain
    if isinstance(intent, RenameTerminal):
        if chain == intent.old_member:
            return intent.new_member
        if not chain.endswith("." + intent.old_member):
            return None
        parts = chain.split(".")
        parts[-1] = intent.new_member
        return ".".join(parts)

    if isinstance(intent, ReplaceResolvedExport):
        if observation.spelling == Spelling.RECEIVER_MEMBER:
            # Owner-changing hops do not rewrite instance receivers.
            return None
        source = ".".join(intent.source)
        target = ".".join(intent.target)
        if not source or not target:
            return None
        if chain == source:
            return target
        if observation.resolved_export and observation.resolved_export == intent.source:
            if chain.endswith("." + source):
                return chain[: -len(source)] + target
            if chain.split(".")[-1] == intent.source[-1] and len(intent.source) == 1:
                return target
        if chain == source or chain.endswith("." + source):
            prefix_len = len(chain) - len(source)
            if prefix_len >= 0 and chain[prefix_len:] == source:
                return chain[:prefix_len] + target
        return None
    return None


def replacement_chain(contract: PacketContract, old_chain: str) -> str | None:
    """Compatibility helper: materialize without a full observation.

    Prefer ``materialize`` with a real observation. This wrapper assumes
    QUALIFIED spelling for dotted chains and IMPORTED for bare names so legacy
    tests keep working.
    """
    intent = _intent_for(contract)
    if intent is None:
        return None
    spelling = (
        Spelling.IMPORTED if "." not in old_chain else Spelling.QUALIFIED
    )
    # Heuristic: dotted chains that are not the export look like receivers —
    # use RECEIVER_MEMBER so RenameTerminal preserves them.
    export = ()
    if isinstance(contract, (SurfaceContract, LexicalOnlyContract)):
        export = contract.export_path
    if (
        isinstance(intent, RenameTerminal)
        and "." in old_chain
        and export
        and old_chain != ".".join(export)
        and old_chain.endswith("." + intent.old_member)
    ):
        spelling = Spelling.RECEIVER_MEMBER
    obs = Observation(
        surface_id=getattr(contract, "surface_id", ""),
        span=SourceSpan(path="", line=0),
        chain=old_chain,
        confidence=Confidence.DEFINITE,
        evidence=MatchEvidence.EXACT,
        spelling=spelling,
        resolved_export=export,
    )
    return materialize(intent, obs)


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
