"""Self-correct context extraction + rate-limit helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conduit.llm.retry import (
    call_with_rate_limit_retry,
    is_rate_limit_error,
    parse_retry_after_seconds,
)
from conduit.self_correct import (
    _collect_context_files,
    _packet_for_prompt,
    _paths_from_traceback,
    _repair_regressed,
)
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


def test_collect_context_keeps_source_impl_over_test_flood(tmp_path: Path):
    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .client import configure\n", encoding="utf-8")
    (pkg / "client.py").write_text("def configure():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("from openai_text.client import configure\n", encoding="utf-8")
    stdout_lines = []
    import_files = ["openai_text/__init__.py", "openai_text/client.py"]
    for i in range(20):
        name = f"test_extra_{i}.py"
        (tests / name).write_text("def test_x(): pass\n", encoding="utf-8")
        stdout_lines.append(f"tests/{name}:1: in test_x\n    assert False\n")
        import_files.append(f"tests/{name}")
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["python", "-m", "pytest", "-q"],
        stdout="".join(stdout_lines),
        stderr="",
    )
    files = _collect_context_files(
        tmp_path,
        result,
        source={"import_files": import_files},
    )
    rels = {k.replace("\\", "/") for k in files}
    assert "openai_text/client.py" in rels
    assert "openai_text/__init__.py" in rels
    assert "tests/conftest.py" in rels


def test_packet_for_prompt_drops_notes():
    slim = _packet_for_prompt(
        {
            "packet_id": "p",
            "package": "openai",
            "from_version": "0.28.1",
            "to_version": "1.0.0",
            "notes": "catalog dump " * 200,
            "rules": [{"type": "DEPENDENCY_BUMP", "package": "openai"}],
        }
    )
    assert "notes" not in slim
    assert slim["rules"]


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


def test_self_correct_reverts_regressed_rewrite(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc

    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    original = "def configure():\n    return True\n"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(original, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "from openai_text.client import configure\n", encoding="utf-8"
    )
    (tests / "test_a.py").write_text("def test_a(): assert False\n", encoding="utf-8")

    broken = "def other():\n    return 1\n"
    results = [
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="8 failed, 10 passed",
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout="ImportError while loading conftest\ncannot import name 'configure'",
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="8 failed, 10 passed",
            stderr="",
        ),
        RunnerResult(
            passed=True,
            returncode=0,
            runner="pytest",
            command=["pytest"],
            stdout="10 passed",
            stderr="",
        ),
    ]

    class FakeClient:
        def run_agent(self, **kwargs):
            user = kwargs.get("user") or ""
            if "regressed" in user or "dropped" in user:
                return {
                    "files": {"openai_text/client.py": original},
                    "packet_patch": {},
                }
            return {
                "files": {"openai_text/client.py": broken},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_tests", lambda _root: results.pop(0))
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], []))

    logs: list[str] = []
    result, _changed = sc.verify_with_self_correct(
        tmp_path,
        {"package": "openai", "rules": [], "notes": ""},
        max_retries=3,
        verbose=True,
        log=logs.append,
    )
    assert any("regressed" in line for line in logs)
    assert (pkg / "client.py").read_text(encoding="utf-8") == original
    assert result.passed


def test_repair_regressed_detects_collection_error():
    prev = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout="8 failed, 10 passed",
        stderr="",
    )
    cur = RunnerResult(
        passed=False,
        returncode=4,
        runner="pytest",
        command=["pytest"],
        stdout="ImportError while loading conftest",
        stderr="",
    )
    assert _repair_regressed(prev, cur) is True


def _empty_anticheat(*_a, **_k):
    from conduit.anticheat.scan import AnticheatReport

    return AnticheatReport(findings=[], source="mechanical")


def test_self_correct_stops_on_initial_anticheat(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc
    from conduit.anticheat.scan import AnticheatReport

    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {"files": {}, "packet_patch": {}}

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(
        sc,
        "run_anticheat",
        lambda *_a, **_k: AnticheatReport(
            findings=["fake sdk stub"], source="mechanical"
        ),
    )
    logs: list[str] = []
    result, changed = sc.verify_with_self_correct(
        tmp_path,
        {"rules": [], "notes": ""},
        max_retries=5,
        log=logs.append,
    )
    assert calls["llm"] == 0
    assert not result.passed
    assert result.runner == "anticheat"
    assert "anticheat failure" in (result.fail_reason or "")
    assert any("anticheat failure (not retryable)" in line for line in logs)
    assert changed == []


def test_self_correct_stops_on_anticheat_after_attempt(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc
    from conduit.anticheat.scan import anticheat_failure_result

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a(): assert False\n", encoding="utf-8"
    )

    fail = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout="FAILED tests/test_a.py::test_a - assert False\n1 failed",
        stderr="",
        failed_count=1,
    )
    cheat = anticheat_failure_result(["dropped openai import"], source="mechanical")
    results = [fail, cheat]
    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {
                "files": {"app.py": "x = 2\n"},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", lambda _root: results.pop(0))
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )

    logs: list[str] = []
    result, _ = sc.verify_with_self_correct(
        tmp_path,
        {"rules": [], "notes": ""},
        max_retries=5,
        log=logs.append,
    )
    assert calls["llm"] == 1
    assert result.runner == "anticheat"
    assert any("anticheat failure (not retryable)" in line for line in logs)
    assert not any("attempt 2/" in line for line in logs)


def test_self_correct_stops_when_all_writes_rejected(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc

    (tmp_path / "app.py").write_text("import openai\nx=1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a(): assert False\n", encoding="utf-8"
    )

    fail = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout="FAILED tests/test_a.py::test_a\n1 failed",
        stderr="",
        failed_count=1,
    )
    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {
                "files": {"app.py": "x = 1\n"},  # drops openai import → reject
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", lambda _root: fail)
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )

    logs: list[str] = []
    result, _ = sc.verify_with_self_correct(
        tmp_path,
        {"package": "openai", "rules": [], "notes": ""},
        max_retries=5,
        log=logs.append,
    )
    assert calls["llm"] == 1
    assert "all repair writes rejected" in (result.fail_reason or "")
    assert any("all repair writes rejected" in line for line in logs)
    assert not any("attempt 2/" in line for line in logs)


def test_self_correct_stops_on_stagnant_fingerprint(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a(): assert False\n", encoding="utf-8"
    )

    same_fail = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "FAILED tests/test_conduit_oracle.py::test_conduit_no_legacy_tokens - "
            "AssertionError: packets/x.json still contains 'davinci'\n"
            "1 failed"
        ),
        stderr="",
        failed_count=1,
    )
    results = [same_fail, same_fail]
    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {
                "files": {"app.py": f"x = {calls['llm']}\n"},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", lambda _root: results.pop(0))
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )

    logs: list[str] = []
    result, _ = sc.verify_with_self_correct(
        tmp_path,
        {"rules": [], "notes": ""},
        max_retries=5,
        log=logs.append,
    )
    assert calls["llm"] == 1
    assert "no progress" in (result.fail_reason or "")
    assert any("same failures for 2 attempts" in line for line in logs)
    assert not any("attempt 2/" in line for line in logs)


def test_self_correct_stops_after_two_consecutive_restores(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc

    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    original = "def configure():\n    return True\n"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(original, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "from openai_text.client import configure\n", encoding="utf-8"
    )
    (tests / "test_a.py").write_text("def test_a(): assert False\n", encoding="utf-8")

    broken = "def other():\n    return 1\n"
    # initial fail, regress, post-restore fail, regress again, post-restore fail
    results = [
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="FAILED tests/test_a.py::test_a\n8 failed, 10 passed",
            stderr="",
            failed_count=8,
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout="ImportError while loading conftest\ncannot import name 'configure'",
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="FAILED tests/test_a.py::test_a\n8 failed, 10 passed",
            stderr="",
            failed_count=8,
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout="ImportError while loading conftest\ncannot import name 'configure'",
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout="FAILED tests/test_a.py::test_a\n8 failed, 10 passed",
            stderr="",
            failed_count=8,
        ),
    ]

    class FakeClient:
        def run_agent(self, **kwargs):
            return {
                "files": {"openai_text/client.py": broken},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", lambda _root: results.pop(0))
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )

    logs: list[str] = []
    result, _ = sc.verify_with_self_correct(
        tmp_path,
        {"package": "openai", "rules": [], "notes": ""},
        max_retries=5,
        log=logs.append,
    )
    assert any("repair regressed twice" in line for line in logs)
    assert "repair regressed twice" in (result.fail_reason or "")
    assert (pkg / "client.py").read_text(encoding="utf-8") == original
    # Should not burn all 5 attempts
    assert sum(1 for line in logs if "attempt " in line and "/" in line) <= 2
