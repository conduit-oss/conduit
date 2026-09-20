"""Evidence-bound surface contracts and consumer binding (PR1)."""

from __future__ import annotations

from conduit.surface.bind import bind, evaluate_binding
from conduit.surface.contracts import contracts_from_packet
from conduit.surface.index import index_python
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
    "bind",
    "contracts_from_packet",
    "evaluate_binding",
    "index_python",
]
