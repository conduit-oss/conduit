"""Smoke test for google_genai module."""

from conduit.detect.modules.google_genai import GoogleGenaiModule


def test_module_name():
    mod = GoogleGenaiModule()
    assert mod.name == "google_genai"
    assert mod.profile is not None
    assert mod.profile.name == "google_genai"
