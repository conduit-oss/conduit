"""Evidence-bound surface contracts and consumer binding (PR1)."""

from __future__ import annotations

from conduit.surface.bind import bind, evaluate_binding
from conduit.surface.contracts import contracts_from_packet
from conduit.surface.evaluate import (
    binding_blocks_complete,
    binding_to_dict,
    evaluate_packet_binding,
)
from conduit.surface.index import index_python
from conduit.surface.mint_surface import derive_surface_floor, enrich_minted_rules
from conduit.surface.rewrite import apply_definite_surface_rewrites, replacement_chain
from conduit.surface.types import (
    BindingVerdict,
    Confidence,
    ConsumerIndex,
    LexicalOnlyContract,
    MatchEvidence,
    Observation,
    PacketContract,
    Spelling,
    SurfaceContract,
    UseKind,
    VerdictStatus,
)

__all__ = [
    "BindingVerdict",
    "Confidence",
    "ConsumerIndex",
    "LexicalOnlyContract",
    "MatchEvidence",
    "Observation",
    "PacketContract",
    "Spelling",
    "SurfaceContract",
    "UseKind",
    "VerdictStatus",
    "apply_definite_surface_rewrites",
    "bind",
    "binding_blocks_complete",
    "binding_to_dict",
    "contracts_from_packet",
    "derive_surface_floor",
    "enrich_minted_rules",
    "evaluate_binding",
    "evaluate_packet_binding",
    "index_python",
    "replacement_chain",
]
