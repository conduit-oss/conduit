"""OpenAI evidence seeds — re-export from VendorProfile for compatibility."""

from __future__ import annotations

from conduit.detect.modules.openai.profile import OPENAI_PROFILE

OPENAI_EVIDENCE_SEEDS: list[str] = list(OPENAI_PROFILE.evidence_seeds)
OPENAI_EVIDENCE_HOSTS: frozenset[str] = frozenset(OPENAI_PROFILE.evidence_hosts)


def openai_evidence_queries(from_version: str, to_version: str) -> list[str]:
    return OPENAI_PROFILE.format_evidence_queries(
        from_version=from_version, to_version=to_version
    )
