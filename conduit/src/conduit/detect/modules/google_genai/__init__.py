"""Detect module: google_genai."""

from __future__ import annotations

from conduit.detect.models import ChangeSignal
from conduit.detect.modules.base import DetectContext, DetectModule
from conduit.detect.modules.google_genai.profile import PROFILE


class GoogleGenaiModule(DetectModule):
    name = "google_genai"
    packages = ["google-generativeai"]
    profile = PROFILE

    def run(self, ctx: DetectContext) -> list[ChangeSignal]:
        # Lazy import: profile_runner pulls openai workers and would circular-import
        # if this module is loaded from discovery before openai finishes init.
        from conduit.detect.profile_runner import run_profile_module, stock_workers_for

        return run_profile_module(
            ctx,
            profile=self.profile,
            workers=stock_workers_for(self.profile),
        )
