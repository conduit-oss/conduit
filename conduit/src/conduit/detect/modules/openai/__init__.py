"""OpenAI detect module — profile-backed workers + compat."""

from __future__ import annotations

from typing import Any

from conduit.detect.client_state import PackageClientState
from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.modules.openai.profile import OPENAI_PROFILE
from conduit.detect.profile_runner import run_profile_module
from conduit.detect.modules.openai.workers import ALL_WORKERS
from conduit.detect.vendor_profile import VendorProfile


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
            items.append(
                "Confirm replacement models are expected: "
                + ", ".join(f"{old} → {new}" for old, new in pairs)
            )
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
