"""Pre-apply impact analysis: mechanical + LLM before packet apply."""

from conduit.patcher.impact.engine import ImpactReport, analyze_impacts

__all__ = ["ImpactReport", "analyze_impacts"]
