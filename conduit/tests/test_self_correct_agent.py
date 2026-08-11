"""Self-correct context extraction + rate-limit helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conduit.llm.retry import (
    call_with_rate_limit_retry,
    is_rate_limit_error,
    parse_retry_after_seconds,
)
from conduit.self_correct import _collect_context_files, _paths_from_traceback
from conduit.test_runner import TestResult as RunnerResult


def test_paths_from_pytest_short_traceback(tmp_path: Path):
    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    eng = pkg / "engines.py"
    eng.write_text("x = 1\n", encoding="utf-8")
    text = (
        "tests/test_engines.py:7: in test_complete\n"
        "    complete_with_engine()\n"
        "openai_text\\engines.py:25: in complete_with_engine\n"
        "    response.raise_for_status()\n"
    )
    found = _paths_from_traceback(tmp_path, text)
    assert eng.resolve() in {p.resolve() for p in found}


def test_collect_context_includes_package_and_import_files(tmp_path: Path):
    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    eng = pkg / "engines.py"
    eng.write_text("ENGINES = True\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_engines.py").write_text("def test_x(): pass\n", encoding="utf-8")

    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["python", "-m", "pytest", "-q"],
        stdout=(
            "openai_text\\engines.py:25: in complete_with_engine\n"
            "E   HTTPError: 404\n"
        ),
        stderr="",
    )
    files = _collect_context_files(
        tmp_path,
        result,
        packet={"import_files": ["openai_text/engines.py"]},
    )
    assert "openai_text/engines.py" in files or "openai_text\\engines.py" in files
    # Normalized keys use forward slashes from _add
    assert any(k.replace("\\", "/") == "openai_text/engines.py" for k in files)


def test_rate_limit_helpers(monkeypatch):
    class Fake429(Exception):
        status_code = 429

    assert is_rate_limit_error(Fake429("rate_limit_exceeded"))
    assert is_rate_limit_error(
        Exception(
            "Error code: 429 - Rate limit reached. Please try again in 22.356s."
        )
    )
    wait = parse_retry_after_seconds(
        Exception("Please try again in 22.356s. Visit platform.openai.com")
    )
    assert 22.0 <= wait <= 23.0

    sleeps: list[float] = []
    monkeypatch.setattr("conduit.llm.retry.time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Fake429("Error code: 429 - Please try again in 0.01s.")
        return "ok"

    assert call_with_rate_limit_retry(flaky, max_retries=4) == "ok"
    assert calls["n"] == 3
    assert sleeps


def test_self_correct_nudge_continues(monkeypatch, tmp_path: Path):
    """Empty LLM attempt with retries left should nudge once instead of stopping."""
    from conduit import self_correct as sc

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): assert False\n", encoding="utf-8")

    results = [
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="FAILED",
            stderr="",
        ),
        RunnerResult(
            passed=True,
            returncode=0,
            runner="pytest",
            command=["pytest"],
            stdout="ok",
            stderr="",
        ),
    ]
    calls = {"llm": 0, "nudge": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            user = kwargs.get("user") or ""
            if "Previous attempt made no file edits" in user:
                calls["nudge"] += 1
                return {
                    "files": {"tests/test_a.py": "def test_a(): assert True\n"},
                    "packet_patch": {},
                }
            return {"files": {}, "packet_patch": {}}

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_tests", lambda _root: results.pop(0))
    monkeypatch.setattr(
        sc,
        "_extract_research_targets",
        lambda *_a, **_k: ([], []),
    )
    monkeypatch.setattr(sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], []))

    packet = {"rules": [], "notes": ""}
    result, changed = sc.verify_with_self_correct(
        tmp_path, packet, max_retries=3, verbose=False
    )
    assert calls["llm"] >= 2
    assert calls["nudge"] == 1
    assert result.passed
    assert any("test_a.py" in c for c in changed)
