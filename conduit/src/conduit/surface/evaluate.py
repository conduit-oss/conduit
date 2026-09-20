"""Run the surface binder against a consumer tree for a packet."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from conduit.patcher.dependency_update import dependency_packages
from conduit.prune.grep_imports import prune_by_imports
from conduit.surface.bind import bind, evaluate_binding
from conduit.surface.contracts import contracts_from_packet
from conduit.surface.index import index_python
from conduit.surface.types import (
    BindingVerdict,
    MatchEvidence,
    Observation,
    VerdictStatus,
)


def evaluate_packet_binding(root: Path, packet: Mapping[str, Any]) -> BindingVerdict:
    """Index + bind + gate for AST_CALL_REWRITE surfaces in ``packet``."""
    contracts = contracts_from_packet(packet)
    if not contracts:
        return BindingVerdict(
            status=VerdictStatus.UNVERIFIED,
            observations=(),
            reasons=("no AST_CALL_REWRITE surfaces declared",),
        )

    pkg = str(packet.get("package") or "").strip()
    packages = dependency_packages(dict(packet)) or ([pkg] if pkg else [])
    packages = [p for p in packages if p]
    files = prune_by_imports(root, packages) if packages else []
    if not files:
        files = list(root.rglob("*.py"))

    index = index_python(root, files)
    observations = bind(contracts, index, pkg)
    return evaluate_binding(contracts, observations)


def binding_to_dict(verdict: BindingVerdict) -> dict[str, Any]:
    return {
        "status": verdict.status.value,
        "observation_count": len(verdict.observations),
        "observations": [_observation_to_dict(o) for o in verdict.observations],
        "reasons": list(verdict.reasons),
        "explain": verdict.explain(),
    }


def _observation_to_dict(o: Observation) -> dict[str, Any]:
    return {
        "surface_id": o.surface_id,
        "path": o.span.path,
        "line": o.span.line,
        "chain": o.chain,
        "confidence": o.confidence.value,
        "evidence": o.evidence.name,
        "spelling": o.spelling.value,
        "disposition": o.disposition,
    }


def binding_blocks_complete(verdict: BindingVerdict) -> bool:
    """True when binder must fail-close apply/Watch at or past to_version.

    - ``incomplete``: proof-eligible unresolved obligations (incl. MEMBER_ONLY).
    - ``unverified``: lexical packets fail only on strong evidence
      (``RECEIVER_TYPED`` and above). Bare ``MEMBER_ONLY`` (e.g. any ``.create``)
      stays report-only so legacy clean trees do not false-fail.
    """
    if verdict.status == VerdictStatus.INCOMPLETE:
        return True
    if verdict.status == VerdictStatus.UNVERIFIED:
        return any(
            o.evidence >= MatchEvidence.RECEIVER_TYPED for o in verdict.observations
        )
    return False
