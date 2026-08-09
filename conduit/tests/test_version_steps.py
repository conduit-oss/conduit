"""Intermediate package/SDK version steps."""

from __future__ import annotations

from packaging.version import Version

from conduit.detect.client_state import PackageClientState
from conduit.detect.modules.openai.normalize import default_rules_for
from conduit.detect.modules.openai.workers.sdk_release import SDKReleaseWorker
from conduit.detect.version_steps import (
    list_release_versions,
    next_version_step,
    version_step_reason,
)


def test_next_version_step_next_major_highest_patch():
    chosen = next_version_step(
        "0.28.1",
        ["1.0.0", "1.40.0", "2.0.0"],
        majors_only=True,
    )
    assert chosen == Version("1.40.0")


def test_next_version_step_all_bumps_next_release():
    chosen = next_version_step(
        "1.0.0",
        ["1.1.0", "2.0.0"],
        majors_only=False,
    )
    assert chosen == Version("1.1.0")


def test_next_version_step_none_when_already_current_major_line():
    # On 1.x with only 1.x ahead still majors_only looks for major+1
    chosen = next_version_step(
        "1.0.0",
        ["1.1.0", "1.40.0"],
        majors_only=True,
    )
    assert chosen is None


def test_list_release_versions_sorted_unique():
    vers = list_release_versions(["v2.0.0", "1.0.0", "v1.0.0", "bad"])
    assert [str(v) for v in vers] == ["1.0.0", "2.0.0"]


def test_version_step_reason_mentions_deferred_latest():
    text = version_step_reason("0.28.1", "1.40.0", "2.0.0", majors_only=True)
    assert "1.40.0" in text
    assert "2.0.0" in text
    assert "deferred" in text.lower()


def test_sdk_demo_defers_latest_major():
    worker = SDKReleaseWorker()
    state = PackageClientState(
        package="openai",
        installed_version="0.28.1",
        ecosystems=["pip"],
    )
    signals = worker.run(demo=True, client_state=state, majors_only=True)
    assert signals
    sig = signals[0]
    assert sig.extra["to_version"] == "1.40.0"
    assert sig.extra["deferred_latest"] == "2.0.0"
    rules = default_rules_for(sig)
    assert rules and rules[0]["type"] == "DEPENDENCY_BUMP"
    assert rules[0]["to_version"] == "1.40.0"
    assert "deferred" in (rules[0].get("reason") or "").lower() or "1.40" in (
        rules[0].get("reason") or ""
    )
