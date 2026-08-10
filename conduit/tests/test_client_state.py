"""Client package state scan + SDKRelease grounded on installed version."""

from __future__ import annotations

from pathlib import Path

from conduit.detect.client_state import PackageClientState, scan_package_state
from conduit.detect.modules.openai.known_models import (
    collect_known_model_ids,
    extract_model_kwarg_ids,
    find_known_models_in_text,
)
from conduit.detect.modules.openai.workers.model_polling import ModelPollingWorker
from conduit.detect.modules.openai.workers.sdk_release import SDKReleaseWorker


def test_scan_finds_model_ids_in_import_file(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.40.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'import openai\nclient.chat.completions.create(model="gpt-4-0613")\n',
        encoding="utf-8",
    )
    state = scan_package_state(
        tmp_path,
        "openai",
        installed={"openai": "1.40.0"},
        demo=True,
        use_llm=False,
    )
    assert state.installed_version == "1.40.0"
    assert "gpt-4-0613" in state.model_ids
    assert any(p.endswith("app.py") for p in state.import_files)
    assert "pip" in state.ecosystems
    assert state.source in {"regex", "demo"}


def test_scan_finds_legacy_completion_models_from_universe(tmp_path: Path):
    """Catalog/deprecation-grounded scan finds ids the static prefix regex misses."""
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import openai\n"
        'openai.Completion.create(model="text-davinci-003")\n'
        'openai.Completion.create(model="davinci")\n'
        'OTHER = "not-a-real-openai-model-xyz"\n',
        encoding="utf-8",
    )
    state = scan_package_state(
        tmp_path,
        "openai",
        installed={"openai": "0.28.1"},
        demo=True,
        use_llm=False,
    )
    assert "text-davinci-003" in state.model_ids
    assert "davinci" in state.model_ids
    assert "not-a-real-openai-model-xyz" not in state.model_ids
    # Longest-first: text-davinci-003 must not collapse to bare davinci only
    assert state.model_ids.count("davinci") == 1


def test_scan_skips_readme_false_positives(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "We used to support gpt-4-0613\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("import openai\n", encoding="utf-8")
    state = scan_package_state(
        tmp_path,
        "openai",
        installed={"openai": "1.0.0"},
        demo=True,
        use_llm=False,
    )
    assert "gpt-4-0613" not in state.model_ids


def test_known_models_longest_first_and_kwarg_filter():
    known = collect_known_model_ids(demo=True)
    assert "text-davinci-003" in known
    assert "davinci" in known
    text = 'model="text-davinci-003" and also davinci and junk-token-99'
    hits = find_known_models_in_text(text, known)
    assert "text-davinci-003" in hits
    assert "davinci" in hits
    assert "junk-token-99" not in hits
    kwargs = extract_model_kwarg_ids(
        'x = dict(model="davinci", other="nope")\n', known
    )
    assert kwargs == {"davinci"}


def test_model_polling_emits_removed_for_discovered_legacy(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==0.28.1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'import openai\nopenai.Completion.create(model="text-davinci-003")\n',
        encoding="utf-8",
    )
    state = scan_package_state(
        tmp_path,
        "openai",
        installed={"openai": "0.28.1"},
        demo=True,
        use_llm=False,
    )
    assert "text-davinci-003" in state.model_ids
    worker = ModelPollingWorker()
    signals = worker.run(demo=True, client_state=state)
    assert any(s.affected_pattern == "text-davinci-003" for s in signals)


def test_llm_enrich_merges_grounded_tokens_only(tmp_path: Path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'import openai\nMODEL = "gpt-4o-mini"\n',
        encoding="utf-8",
    )

    class FakeLLM:
        def complete_json(self, *, system: str, user: str):
            return {
                "model_ids": ["gpt-4o-mini", "gpt-invented-not-in-snippets"],
                "api_patterns": ["chat.completions"],
            }

    monkeypatch.setattr(
        "conduit.llm.client.get_llm_client",
        lambda: FakeLLM(),
    )
    # Avoid live catalog/deprecation fetches in this unit test.
    monkeypatch.setattr(
        "conduit.detect.modules.openai.known_models.collect_known_model_ids",
        lambda **kwargs: {"gpt-4o-mini"},
    )
    state = scan_package_state(
        tmp_path,
        "openai",
        installed={"openai": "1.0.0"},
        demo=False,
        use_llm=True,
    )
    assert "gpt-4o-mini" in state.model_ids
    assert "gpt-invented-not-in-snippets" not in state.model_ids
    assert state.source == "regex+llm"


def test_sdk_release_compares_installed_to_latest(monkeypatch):
    monkeypatch.setattr(
        "conduit.detect.modules.openai.workers.sdk_release._github_release_tags",
        lambda repo, **kwargs: (
            ["v1.0.0", "v1.40.0", "v2.0.0"] if "python" in repo else []
        ),
    )
    worker = SDKReleaseWorker()
    state = PackageClientState(
        package="openai",
        installed_version="0.28.1",
        ecosystems=["pip"],
    )
    signals = worker.run(demo=False, client_state=state)
    assert len(signals) == 1
    assert signals[0].extra["from_version"] == "0.28.1"
    # Next major line is 1.x (highest 1.40.0), not latest 2.0.0
    assert signals[0].extra["to_version"] == "1.40.0"
    assert signals[0].extra.get("deferred_latest") == "2.0.0"


def test_sdk_release_skips_without_installed():
    worker = SDKReleaseWorker()
    assert worker.run(demo=False, client_state=None) == []
    assert worker.last_skip_reason == "no_installed_version"


def test_sdk_release_demo_uses_fixture_latest():
    worker = SDKReleaseWorker()
    state = PackageClientState(
        package="openai",
        installed_version="0.28.1",
        ecosystems=["pip"],
    )
    signals = worker.run(demo=True, client_state=state)
    assert signals
    assert signals[0].extra["to_version"] == "1.40.0"
    assert signals[0].extra.get("deferred_latest") == "2.0.0"
    assert "deferred" in (signals[0].description or "").lower() or "2.0" in (
        signals[0].extra.get("reason") or ""
    )
