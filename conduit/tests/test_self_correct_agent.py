"""Self-correct context extraction + rate-limit helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from conduit.llm.retry import (
    call_with_rate_limit_retry,
    is_rate_limit_error,
    parse_retry_after_seconds,
)
from conduit.self_correct import (
    _PROMPT_STREAM_HEAD,
    _PROMPT_STREAM_TAIL,
    _collect_context_files,
    _file_window,
    _packet_for_prompt,
    _paths_from_traceback,
    _repair_regressed,
    _structured_failure,
    collect_repair_context,
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


def test_collect_context_caps_windows_not_import_flood(tmp_path: Path):
    pkg = tmp_path / "openai_text"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .client import configure\n", encoding="utf-8")
    (pkg / "client.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 200)) + "\n",
        encoding="utf-8",
    )
    # Extra impl modules listed in import_files but NOT in traceback.
    flood = []
    for i in range(20):
        name = f"extra_{i}.py"
        (pkg / name).write_text(f"X{i} = 1\n", encoding="utf-8")
        flood.append(f"openai_text/{name}")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("from openai_text.client import configure\n", encoding="utf-8")
    (tests / "test_client.py").write_text("def test_x(): assert False\n", encoding="utf-8")
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["python", "-m", "pytest", "-q"],
        stdout=(
            "FAILED tests/test_client.py::test_x\n"
            "tests/test_client.py:1: in test_x\n"
            "    assert False\n"
            "openai_text\\client.py:100: in configure\n"
            "E   AssertionError\n"
        ),
        stderr="",
    )
    ctx = collect_repair_context(
        tmp_path,
        result,
        source={"import_files": ["openai_text/__init__.py", "openai_text/client.py", *flood]},
    )
    rels = {k.replace("\\", "/") for k in ctx.files}
    assert "openai_text/client.py" in rels
    assert "tests/conftest.py" in rels
    assert "tests/test_client.py" in rels
    # Flood import_files not in traceback must not fill the seed set.
    assert not any(r.startswith("openai_text/extra_") for r in rels)
    impl_windows = [
        w for w in ctx.file_windows if not str(w.get("path", "")).startswith("tests/")
    ]
    assert len(impl_windows) <= 8
    # Span window around line 100, not the whole 200-line file.
    client_win = next(w for w in ctx.file_windows if w["path"] == "openai_text/client.py")
    assert client_win["start"] <= 100 <= client_win["end"]
    assert "line_100" in client_win["text"]
    assert "line_1 =" not in client_win["text"] or client_win["start"] == 1


def test_file_window_radius(tmp_path: Path):
    path = tmp_path / "mod.py"
    path.write_text("\n".join(f"L{i}" for i in range(1, 201)) + "\n", encoding="utf-8")
    win = _file_window(path, 100, radius=60)
    assert win["start"] == 40
    assert win["end"] == 160
    assert "L100" in win["text"]
    assert "L1\n" not in win["text"]


def test_structured_failure_fields():
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "FAILED tests/test_x.py::test_a - AssertionError\n"
            "E   AssertionError: still contains 'ChatCompletion'\n"
        ),
        stderr="",
        failed_count=1,
        passed_count=0,
        error_count=0,
        fail_reason="1 failed",
    )
    structured = _structured_failure(result)
    assert structured["failed_nodes"] == ["tests/test_x.py::test_a"]
    assert "ChatCompletion" in structured["leftover_tokens"]
    assert "nodes=" in structured["failure_fingerprint"]
    assert structured["failure_digest"]


def test_failure_digest_keeps_early_failed_and_exception_snippets():
    from conduit.self_correct import build_failure_digest, _pack_stream

    early = "FAILED tests/test_early.py::test_one\n"
    mid = ("x" * 5000) + "\n"
    late = (
        "ERROR tests/test_late.py::test_two\n"
        "openai.BadRequestError: Unsupported value: 'temperature' does not support 0\n"
        "E   BadRequestError: Unsupported value: 'temperature' does not support 0\n"
        "===== 2 failed =====\n"
    )
    blob = early + mid + late
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=blob,
        stderr="",
        failed_count=2,
    )
    digest = build_failure_digest(result)
    assert "tests/test_early.py::test_one" in digest["failed_nodes"]
    assert "tests/test_late.py::test_two" in digest["failed_nodes"]
    assert any("temperature" in s for s in digest["exception_snippets"])
    assert "temperature" in digest["text"]
    packed = _pack_stream(blob)
    assert "tests/test_early.py::test_one" in packed
    assert "===== 2 failed =====" in packed
    assert "\n...\n" in packed
    structured = _structured_failure(result)
    assert "temperature" in structured["failure_digest"]


def test_structured_failure_fingerprint_uses_snippets_without_nodes():
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            ".....F\n"
            "openai.BadRequestError: Unsupported value: 'temperature' does not support 0 "
            "with this model.\n"
        ),
        stderr="",
        failed_count=1,
        fail_reason="",
    )
    structured = _structured_failure(result)
    assert structured["failed_nodes"] == []
    assert "snippets=" in structured["failure_fingerprint"]
    assert any("temperature" in s for s in structured["exception_snippets"])


def test_collect_repair_context_seeds_leftover_oracle_paths(tmp_path: Path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "deployments.json").write_text(
        '{"model": "text-davinci-003"}\n', encoding="utf-8"
    )
    scripts = tmp_path / "scripts" / "ops"
    scripts.mkdir(parents=True)
    (scripts / "legacy_openai_smoke.sh").write_text(
        "curl /v1/engines\n", encoding="utf-8"
    )
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "FAILED tests/test_conduit_oracle.py::test_conduit_no_legacy_tokens\n"
            "E   assert not [...]\n"
            "configs/deployments.json still contains 'text-davinci-003'\n"
            "scripts/ops/legacy_openai_smoke.sh still contains '/v1/engines'\n"
        ),
        stderr="",
    )
    ctx = collect_repair_context(tmp_path, result)
    paths = {x["path"] for x in ctx.leftover_files}
    assert "configs/deployments.json" in paths
    assert "scripts/ops/legacy_openai_smoke.sh" in paths
    assert "configs/deployments.json" in ctx.allowlist
    assert "scripts/ops/legacy_openai_smoke.sh" in ctx.seeded_paths
    assert "text-davinci-003" in ctx.files.get("configs/deployments.json", "")


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
    monkeypatch.setattr(
        "conduit.patcher.post_rules.synthesize.synthesize_post_rules",
        lambda *_a, **_k: [],
    )

    def _anticheat_then_tests(root, packet, **kwargs):
        return sc.run_tests(root)

    monkeypatch.setattr(sc, "_run_anticheat_then_tests", _anticheat_then_tests)

    def _run_tests(_root, **_k):
        if results:
            return results.pop(0)
        return RunnerResult(
            passed=True,
            returncode=0,
            runner="pytest",
            command=["pytest"],
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(sc, "run_tests", _run_tests)
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
    (tests / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
    )

    # Keep public name ``configure`` (rename would be rejected as non-SDK structure)
    # but break the module so conftest collection fails — triggers restore path.
    broken = (
        "def configure():\n"
        "    return True\n"
        "\n"
        "raise ImportError('broken repair')\n"
    )
    results = [
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "openai_text\\client.py:1: in configure\n"
                "    return True\n"
                "8 failed, 10 passed"
            ),
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "ImportError while loading conftest\n"
                "ImportError: broken repair"
            ),
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "openai_text\\client.py:1: in configure\n"
                "    return True\n"
                "8 failed, 10 passed"
            ),
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
    monkeypatch.setattr(sc, "run_tests", lambda _root, **_k: results.pop(0))
    monkeypatch.setattr(sc, "_run_anticheat_then_tests", lambda root, packet, **kwargs: sc.run_tests(root))
    monkeypatch.setattr(
        "conduit.patcher.post_rules.synthesize.synthesize_post_rules",
        lambda *_a, **_k: [],
    )
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

    return AnticheatReport(source="mechanical")


def test_self_correct_retries_on_initial_anticheat(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc
    from conduit.anticheat.findings import AnticheatFinding
    from conduit.anticheat.scan import AnticheatReport

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {"files": {"app.py": "x = 2\n"}, "packet_patch": {}}

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(
        sc,
        "run_anticheat",
        lambda *_a, **_k: AnticheatReport(
            structured=[
                AnticheatFinding(
                    path="app.py",
                    kind="fake_client",
                    detail="fake sdk stub",
                    severity="block",
                )
            ],
            source="mechanical",
        ),
    )
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )
    logs: list[str] = []
    result, _changed = sc.verify_with_self_correct(
        tmp_path,
        {"rules": [], "notes": ""},
        max_retries=2,
        log=logs.append,
    )
    assert calls["llm"] >= 1
    assert not result.passed
    assert result.runner == "anticheat"
    assert "anticheat failed" in (result.stdout or "")
    assert not any("not retryable" in line for line in logs)
    assert any("attempt 1/" in line for line in logs)


def test_self_correct_retries_anticheat_after_attempt(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc
    from conduit.anticheat.scan import anticheat_failure_result

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a(): assert False\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
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
    results = [fail]

    def _run_tests(_root, **_k):
        if results:
            return results.pop(0)
        return cheat

    calls = {"llm": 0}

    class FakeClient:
        def run_agent(self, **kwargs):
            calls["llm"] += 1
            return {
                "files": {"app.py": f"x = {calls['llm'] + 1}\n"},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", _run_tests)
    monkeypatch.setattr(sc, "_extract_research_targets", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        sc, "_heuristic_fix", lambda *_a, **_k: sc.FixAttempt("heuristic", [], [])
    )

    logs: list[str] = []
    result, _ = sc.verify_with_self_correct(
        tmp_path,
        {"rules": [], "notes": ""},
        max_retries=3,
        log=logs.append,
    )
    assert calls["llm"] >= 2
    assert result.runner == "anticheat"
    assert not any("not retryable" in line for line in logs)
    assert any("attempt 2/" in line for line in logs)


def test_self_correct_stops_when_all_writes_rejected(monkeypatch, tmp_path: Path):
    from conduit import self_correct as sc

    (tmp_path / "app.py").write_text("import openai\nx=1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a(): assert False\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
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
                # Still references openai. after dropping the import → reject_write
                "files": {"app.py": "openai.ChatCompletion.create()\n"},
                "packet_patch": {},
            }

    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "run_anticheat", _empty_anticheat)
    monkeypatch.setattr(sc, "run_tests", lambda _root, **_k: fail)
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
    (tmp_path / "tests" / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
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
    monkeypatch.setattr(sc, "run_tests", lambda _root, **_k: results.pop(0))
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
    (tests / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
    )

    # Keep ``configure``; module-level ImportError still causes collection failure.
    broken = (
        "def configure():\n"
        "    return True\n"
        "\n"
        "raise ImportError('broken repair')\n"
    )
    # initial fail, regress, post-restore fail, regress again, post-restore fail
    results = [
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "openai_text\\client.py:1: in configure\n"
                "FAILED tests/test_a.py::test_a\n8 failed, 10 passed"
            ),
            stderr="",
            failed_count=8,
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "ImportError while loading conftest\n"
                "ImportError: broken repair"
            ),
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "openai_text\\client.py:1: in configure\n"
                "FAILED tests/test_a.py::test_a\n8 failed, 10 passed"
            ),
            stderr="",
            failed_count=8,
        ),
        RunnerResult(
            passed=False,
            returncode=4,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "ImportError while loading conftest\n"
                "ImportError: broken repair"
            ),
            stderr="",
        ),
        RunnerResult(
            passed=False,
            returncode=1,
            runner="pytest",
            command=["pytest"],
            stdout=(
                "openai_text\\client.py:1: in configure\n"
                "FAILED tests/test_a.py::test_a\n8 failed, 10 passed"
            ),
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
    monkeypatch.setattr(sc, "run_tests", lambda _root, **_k: results.pop(0))
    monkeypatch.setattr(
        sc,
        "_run_anticheat_then_tests",
        lambda root, packet, **kwargs: sc.run_tests(root),
    )
    monkeypatch.setattr(
        "conduit.patcher.post_rules.synthesize.synthesize_post_rules",
        lambda *_a, **_k: [],
    )
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


def test_llm_suggest_fixes_prompt_has_journal_and_structured(tmp_path: Path, monkeypatch):
    from conduit.anticheat.audit_log import MigrationAuditLog
    from conduit import self_correct as sc

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    captured: dict = {}

    class FakeClient:
        def run_agent(self, **kwargs):
            captured["user"] = kwargs.get("user") or ""
            return {"files": {}, "packet_patch": {}}

    audit = MigrationAuditLog.from_packet({"packet_id": "t", "package": "openai"})
    audit.record_reject("shim.py", "fake client", attempt=1)
    result = RunnerResult(
        passed=False,
        returncode=1,
        runner="pytest",
        command=["pytest"],
        stdout=(
            "FAILED tests/test_app.py::test_x\n"
            "app.py:1: in <module>\n"
            "E   still contains 'Legacy'\n"
        ),
        stderr="",
        failed_count=1,
    )
    monkeypatch.setattr(sc, "get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "attach_llm_log", lambda client, _log: client)

    suggestion = sc._llm_suggest_fixes(
        root=tmp_path,
        test_result=result,
        packet={"package": "openai", "rules": [], "to_version": "1.0.0"},
        files={"app.py": "x = 1\n"},
        audit_log=audit,
        attempt=2,
        file_windows=[
            {"path": "app.py", "start": 1, "end": 1, "line": 1, "text": "x = 1\n"}
        ],
        path_allowlist={"app.py"},
        seeded_paths=["app.py"],
    )
    assert isinstance(suggestion, sc.LlmRepairSuggestion)
    prompt = json.loads(captured["user"])
    assert "repair_journal" in prompt
    assert any(e.get("phase") == "reject" for e in prompt["repair_journal"]["entries"])
    assert prompt["failed_nodes"] == ["tests/test_app.py::test_x"]
    assert "Legacy" in prompt["leftover_tokens"]
    assert prompt["failure_fingerprint"]
    assert prompt["failure_digest"]
    assert "exception_snippets" in prompt
    # head+tail packing: early content preserved within budget
    assert len(prompt["error_stdout"]) <= _PROMPT_STREAM_HEAD + _PROMPT_STREAM_TAIL + 10
    assert "file_windows" in prompt
    assert "Edit-first" in prompt["instructions"] or "seeded_paths" in prompt["instructions"]
