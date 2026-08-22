"""Successor selection: map-only modern callees + verb pairing."""

from __future__ import annotations

from conduit.detect.modules.openai.path_callees import modern_callees_for_path
from conduit.detect.modules.openai.sdk_callee_migration import (
    _same_path_legacy_pairs,
    pick_modern_callee,
)


def test_modern_callees_for_path_ignores_usage_order():
    targets = modern_callees_for_path("/v1/completions")
    assert targets[0] == "completions.create"
    assert "Completion.create" in targets


def test_pick_modern_callee_prefers_matching_verb():
    candidates = modern_callees_for_path("/v1/fine_tuning/jobs")
    assert pick_modern_callee("FineTune.list", candidates) == "fine_tuning.jobs.list"
    assert pick_modern_callee("FineTune.create", candidates) == "fine_tuning.jobs.create"


def test_pick_modern_callee_skips_legacy_aliases():
    candidates = modern_callees_for_path("/v1/completions")
    assert pick_modern_callee("openai.Completion.create", candidates) == "completions.create"


def test_same_path_legacy_pairs_not_self_rewrite():
    pairs = _same_path_legacy_pairs(
        ["Completion.create", "openai.Completion.create"],
    )
    by_old = {old: new for old, new, _ in pairs}
    assert by_old["Completion.create"] == "completions.create"
    assert by_old["openai.Completion.create"] == "completions.create"
    assert "Completion.create" not in by_old.values()
