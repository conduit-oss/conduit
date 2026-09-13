"""OpenAI detect module — profile-backed workers + compat."""

from __future__ import annotations

import re
from typing import Any

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.modules.openai.profile import OPENAI_PROFILE
from conduit.detect.profile_runner import run_profile_module
from conduit.detect.modules.openai.workers import ALL_WORKERS
from conduit.detect.vendor_profile import VendorProfile

_SHUTDOWN_RE = re.compile(r"shutdown\s+(\d{4}-\d{2}-\d{2})", re.I)


def _is_profile_model_id(token: str, profile: VendorProfile) -> bool:
    text = (token or "").strip()
    if not text:
        return False
    matcher = profile.model_id_re()
    if matcher is None:
        return False
    hit = matcher.fullmatch(text) or matcher.search(text)
    return bool(hit and hit.group(0).lower() == text.lower())


def _model_replacements(
    packet: dict[str, Any], *, profile: VendorProfile
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("type") != "EXACT_STRING_REPLACE":
            continue
        match = str(rule.get("match") or "").strip()
        replace = str(rule.get("replace") or "").strip()
        if not (
            _is_profile_model_id(match, profile)
            and _is_profile_model_id(replace, profile)
        ):
            continue
        key = (match, replace)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def _deadline_for_model(
    model_id: str,
    *,
    packet: dict[str, Any],
    coverage: Any | None,
) -> str | None:
    mid = model_id.lower()
    if coverage is not None:
        for item in getattr(coverage, "items", None) or []:
            if getattr(item, "kind", None) != "model":
                continue
            if str(getattr(item, "value", "")).lower() != mid:
                continue
            m = _SHUTDOWN_RE.search(str(getattr(item, "detail", "") or ""))
            if m:
                return m.group(1)
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("match") or "").lower() != mid:
            continue
        m = _SHUTDOWN_RE.search(str(rule.get("reason") or ""))
        if m:
            return m.group(1)
    return None


def format_model_auto_update_line(
    packet: dict[str, Any],
    changes: list[Any] | None = None,
    *,
    profile: VendorProfile | None = None,
) -> str | None:
    """Console line listing model ids auto-updated by apply, or None."""
    prof = profile or OPENAI_PROFILE
    pairs = _model_replacements(packet, profile=prof)
    if not pairs:
        return None
    applied: list[tuple[str, str]] = []
    if changes:
        for change in changes:
            detail = str(getattr(change, "detail", "") or "")
            rule_type = str(getattr(change, "rule_type", "") or "")
            if rule_type and rule_type != "EXACT_STRING_REPLACE":
                continue
            for old, new in pairs:
                if old in detail and (old, new) not in applied:
                    applied.append((old, new))
    else:
        applied = list(pairs)
    if not applied:
        return None
    body = ", ".join(f"{old} → {new}" for old, new in applied)
    return f"[models] auto-updating deprecated ids: {body}"


class OpenAIModule(DetectModule):
    name = "openai"
    packages = ["openai"]
    profile = OPENAI_PROFILE

    def review_checklist(
        self,
        *,
        packet: dict[str, Any],
        report: Any,
        state: PackageClientState | None = None,
        coverage: Any = None,
    ) -> list[str]:
        items: list[str] = []
        pairs = _model_replacements(packet, profile=self.profile or OPENAI_PROFILE)
        if pairs:
            bits: list[str] = []
            for old, new in pairs:
                deadline = _deadline_for_model(old, packet=packet, coverage=coverage)
                if deadline:
                    bits.append(f"{old} → {new} (shutdown {deadline})")
                else:
                    bits.append(f"{old} → {new}")
            items.append("Auto-updating deprecated models: " + ", ".join(bits))
            baseline = {m.lower() for m in (state.model_ids if state else [])}
            added = [
                new
                for _, new in pairs
                if new.lower() not in baseline
            ]
            unique_added: list[str] = []
            for model in added:
                if model not in unique_added:
                    unique_added.append(model)
            if unique_added:
                items.append(
                    "Models added (not in client baseline): "
                    + ", ".join(unique_added)
                    + " — confirm these are the models you want"
                )
        elif state and state.model_ids:
            items.append(
                "No model string replacements in the packet; "
                "confirm client model ids were left as intended: "
                + ", ".join(state.model_ids)
            )
        return items

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        prof = self.profile or OPENAI_PROFILE
        return run_profile_module(
            ctx,
            profile=prof,
            workers=list(ALL_WORKERS),
        )
