"""ModelPollingWorker empty-result warnings (missing key vs healthy poll)."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.client_state import PackageClientState
from conduit.detect.modules.base import DetectContext
from conduit.detect.modules.openai import OpenAIModule
from conduit.detect.modules.openai.workers.model_polling import ModelPollingWorker


def test_model_polling_missing_api_key_sets_skip_reason(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    worker = ModelPollingWorker()
    state = PackageClientState(package="openai", model_ids=["gpt-4o"])
    assert worker.run(demo=False, client_state=state) == []
    assert worker.last_skip_reason == "missing_api_key"


def test_model_polling_no_client_models_skip_reason():
    worker = ModelPollingWorker()
    assert worker.run(demo=False, client_state=None) == []
    assert worker.last_skip_reason == "no_client_models"


def test_model_polling_fetch_failed_sets_skip_reason(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "conduit.detect.modules.openai.workers.model_polling._fetch_live_models",
        lambda _key, **_kwargs: None,
    )
    worker = ModelPollingWorker()
    state = PackageClientState(package="openai", model_ids=["gpt-4o"])
    assert worker.run(demo=False, client_state=state) == []
    assert worker.last_skip_reason == "fetch_failed"


def test_model_polling_client_missing_from_catalog(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "conduit.detect.modules.openai.workers.model_polling._fetch_live_models",
        lambda _key, **_kwargs: {"data": [{"id": "gpt-4o"}]},
    )
    worker = ModelPollingWorker()
    state = PackageClientState(
        package="openai", model_ids=["gpt-4o", "gpt-4-0314"]
    )
    signals = worker.run(demo=False, client_state=state)
    assert len(signals) == 1
    assert signals[0].affected_pattern == "gpt-4-0314"


def test_model_polling_success_no_removals_is_verbose_only(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "conduit.detect.modules.openai.workers.model_polling._fetch_live_models",
        lambda _key, **_kwargs: {"data": [{"id": "gpt-4o"}]},
    )
    monkeypatch.setattr(
        "conduit.detect.modules.openai.ALL_WORKERS",
        [ModelPollingWorker],
    )
    ctx = DetectContext(
        repo_root=tmp_path,
        installed={"openai": "1.0.0"},
        package_states={
            "openai": PackageClientState(package="openai", model_ids=["gpt-4o"])
        },
        demo=False,
    )
    OpenAIModule().run(ctx)
    assert ctx.extra.get("warnings") == []
    assert any(
        "ModelPollingWorker: returned 0 signals" in w
        for w in ctx.extra.get("verbose_warnings") or []
    )


def test_model_polling_missing_key_always_warns(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "conduit.detect.modules.openai.ALL_WORKERS",
        [ModelPollingWorker],
    )
    ctx = DetectContext(
        repo_root=tmp_path,
        installed={"openai": "1.0.0"},
        package_states={
            "openai": PackageClientState(package="openai", model_ids=["gpt-4o"])
        },
        demo=False,
    )
    OpenAIModule().run(ctx)
    warnings = ctx.extra.get("warnings") or []
    assert any("set OPENAI_API_KEY" in w for w in warnings)
    assert not (ctx.extra.get("verbose_warnings") or [])
