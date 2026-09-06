"""Verify/self-correct must not treat skip, dummy helpers, or token theater as success."""

from __future__ import annotations

from pathlib import Path

from conduit.credentials import (
    CredentialsError,
    ensure_verify_credentials,
    load_consumer_env,
    needs_openai_consumer_key,
)
from conduit.integrity import integrity_findings, unused_marker_literals
from conduit.patcher.engine import apply_packet
from conduit.repair_ignore import build_ignore_list
from conduit.self_correct import reject_self_correct_write
from conduit.test_gen import ensure_tests, oracle_scan_rels
from conduit.test_runner import (
    TestResult,
    evaluate_pytest_result,
    parse_pytest_counts,
    run_tests,
)


def _packet() -> dict:
    return {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "ada",
                "replace": "text-embedding-3-small",
                "target_files": ["*.py"],
            },
            {
                "type": "AST_CALL_REWRITE",
                "old_callee": "ChatCompletion.create",
                "new_callee": "chat.completions.create",
                "target_files": ["*.py"],
            },
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "/v1/fine-tunes",
                "replace": "/v1/fine_tuning/jobs",
                "target_files": ["*.py"],
            },
        ],
    }


def test_parse_pytest_skip_all_is_failure():
    passed, reason, counts = evaluate_pytest_result(
        returncode=0,
        stdout="===== 5 skipped in 0.04s =====\n",
        stderr="",
    )
    assert passed is False
    assert reason == "all tests skipped"
    assert counts["skipped"] == 5
    assert counts["passed"] == 0


def test_parse_pytest_missing_key_is_failure():
    passed, reason, _counts = evaluate_pytest_result(
        returncode=0,
        stdout="SKIPPED tests/test_chat.py: OPENAI_API_KEY (or OPENAI_KEY) is not set\n"
        "===== 3 skipped in 0.01s =====\n",
        stderr="",
    )
    assert passed is False
    assert "OPENAI_API_KEY" in reason


def test_parse_pytest_passed_with_skips_ok():
    passed, reason, counts = evaluate_pytest_result(
        returncode=0,
        stdout="===== 2 passed, 1 skipped in 0.10s =====\n",
        stderr="",
    )
    assert passed is True
    assert reason == ""
    assert counts["passed"] == 2


def test_parse_pytest_counts_helper():
    counts = parse_pytest_counts("===== 1 failed, 2 passed, 3 skipped, 1 error in 1s =====")
    assert counts["failed"] == 1
    assert counts["passed"] == 2
    assert counts["skipped"] == 3
    assert counts["error"] == 1


def test_run_tests_skip_all_conftest(tmp_path: Path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import os, pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def openai_configured():\n"
        "    if not os.environ.get('OPENAI_API_KEY'):\n"
        "        pytest.skip('OPENAI_API_KEY (or OPENAI_KEY) is not set')\n",
        encoding="utf-8",
    )
    (tests / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    result = run_tests(tmp_path)
    assert result.passed is False
    assert "skip" in (result.fail_reason or "").lower() or "OPENAI_API_KEY" in (
        result.fail_reason or ""
    )


def test_credentials_noninteractive_missing_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "OPENAI_API_KEY = True\n", encoding="utf-8"
    )
    try:
        ensure_verify_credentials(
            tmp_path, {"package": "openai"}, want_llm=False, interactive=False
        )
        raise AssertionError("expected CredentialsError")
    except CredentialsError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_credentials_prompt_sets_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    ensure_verify_credentials(
        tmp_path,
        {"package": "openai"},
        want_llm=False,
        interactive=True,
        prompt=lambda _label: "sk-from-prompt",
    )
    assert monkeypatch is not None
    import os

    assert os.environ.get("OPENAI_API_KEY") == "sk-from-prompt"


def test_load_env_openai_key_maps_to_api_key(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_KEY=sk-from-dotenv\n", encoding="utf-8")
    loaded = load_consumer_env(tmp_path)
    assert "OPENAI_KEY" in loaded or "OPENAI_API_KEY" in loaded
    assert os.environ.get("OPENAI_API_KEY") == "sk-from-dotenv"
    assert os.environ.get("OPENAI_KEY") == "sk-from-dotenv"


def test_load_env_does_not_override_exported(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.setenv("OPENAI_API_KEY", "sk-exported")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dotenv\n", encoding="utf-8")
    load_consumer_env(tmp_path)
    assert os.environ.get("OPENAI_API_KEY") == "sk-exported"


def test_credentials_uses_env_without_prompt(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_KEY=sk-dotenv\n", encoding="utf-8")
    prompted = {"count": 0}

    def _prompt(_label: str) -> str:
        prompted["count"] += 1
        return "should-not-run"

    ensure_verify_credentials(
        tmp_path,
        {"package": "openai"},
        want_llm=False,
        interactive=True,
        prompt=_prompt,
    )
    assert prompted["count"] == 0
    assert os.environ.get("OPENAI_API_KEY") == "sk-dotenv"


def test_credentials_prompt_pauses_pulse(tmp_path: Path, monkeypatch):
    from conduit import pulse

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_KEY", raising=False)
    calls: list[str] = []

    monkeypatch.setattr(pulse, "pause_pulse", lambda: calls.append("pause"))
    monkeypatch.setattr(
        pulse, "resume_pulse", lambda _console: calls.append("resume")
    )
    from conduit.credentials import _prompt_or_fail

    _prompt_or_fail(
        "OPENAI_API_KEY",
        interactive=True,
        prompt=lambda _label: "sk-x",
        log=lambda msg: calls.append("log"),
        console=object(),
    )
    assert calls == ["pause", "log", "resume"]


def test_needs_key_from_conftest(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("os.environ.get('OPENAI_API_KEY')\n", encoding="utf-8")
    assert needs_openai_consumer_key(tmp_path, {"package": "demo"}) is True
    assert needs_openai_consumer_key(tmp_path, {"package": "openai"}) is True


def test_integrity_dummy_except(tmp_path: Path):
    (tmp_path / "chat.py").write_text(
        "def generate_reply(message):\n"
        "    try:\n"
        "        return call()\n"
        "    except Exception:\n"
        "        return message.strip() or 'ok'\n",
        encoding="utf-8",
    )
    findings = integrity_findings(tmp_path, _packet(), ["chat.py"])
    assert any("dummy" in f or "swallows Exception" in f for f in findings)


def test_integrity_dummy_except_allows_legitimate_returns(tmp_path: Path):
    from conduit.integrity import dummy_except_findings

    clean = (
        "def view(request):\n"
        "    try:\n"
        "        return work()\n"
        "    except Exception as e:\n"
        "        return self.handle_exception(e)\n"
        "\n"
        "def redirect_missing(slug):\n"
        "    try:\n"
        "        return Project.objects.get(slug=slug)\n"
        "    except Exception:\n"
        "        return HttpResponseRedirect('/404')\n"
        "\n"
        "def parse(section):\n"
        "    data = {}\n"
        "    try:\n"
        "        data['x'] = section\n"
        "    except Exception:\n"
        "        return data\n"
    )
    assert dummy_except_findings(clean, "views.py") == []

    dirty = (
        "def bad():\n"
        "    try:\n"
        "        return call()\n"
        "    except Exception:\n"
        "        return {}\n"
        "\n"
        "def also_bad():\n"
        "    try:\n"
        "        return call()\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def stub_ok():\n"
        "    try:\n"
        "        return call()\n"
        "    except Exception:\n"
        "        return 'ok'\n"
    )
    hits = dummy_except_findings(dirty, "cheat.py")
    assert len(hits) >= 3


def test_integrity_migration_markers(tmp_path: Path):
    (tmp_path / "client.py").write_text(
        "MIGRATION_MARKERS = (\n"
        "    'chat.completions.create',\n"
        "    'gpt-realtime-2.1',\n"
        ")\n",
        encoding="utf-8",
    )
    findings = integrity_findings(tmp_path, _packet(), ["client.py"])
    assert any("marker" in f.lower() for f in findings)
    hidden = unused_marker_literals(
        (tmp_path / "client.py").read_text(encoding="utf-8"),
        ["chat.completions.create"],
    )
    assert hidden


def test_join_obfuscation_in_impl_not_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "none")
    app = tmp_path / "completions.py"
    app.write_text('LEGACY_ADA = "".join(["a", "da"])\n', encoding="utf-8")
    packet = _packet()
    ignore = build_ignore_list(tmp_path, packet)
    assert not ignore.path_ignored("completions.py")
    rels = oracle_scan_rels(tmp_path, packet, file_allowlist=[app], ignore=ignore)
    assert "completions.py" in rels
    reason = reject_self_correct_write("completions.py", app.read_text(encoding="utf-8"), packet=packet)
    assert reason is not None
    assert "obfuscat" in reason


def test_packet_apply_skips_generated_oracle(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    oracle = tests / "test_conduit_oracle.py"
    original = 'FORBIDDEN = ["ada", "gpt-4"]\n'
    oracle.write_text(original, encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 'ok'\n", encoding="utf-8")
    apply_packet(tmp_path, _packet(), require_context=False)
    assert oracle.read_text(encoding="utf-8") == original


def test_reject_skip_and_smoke_edits():
    packet = _packet()
    assert reject_self_correct_write(
        "tests/test_app.py",
        "import pytest\npytest.skip('nope')\n",
        packet=packet,
    )
    assert reject_self_correct_write(
        "tests/test_conduit_smoke.py",
        "def test_x():\n    assert True\n",
        packet=packet,
    )
    assert reject_self_correct_write(
        "tests/test_conduit_functional.py",
        "def test_x():\n    assert True\n",
        packet=packet,
    )


def test_run_tests_no_suite_is_failure(tmp_path: Path):
    result = run_tests(tmp_path)
    assert isinstance(result, TestResult)
    assert result.passed is False
    assert "no test suite" in result.fail_reason


def test_classify_verify_failure_kinds():
    from conduit.test_runner import classify_verify_failure

    pkt = {"package": "openai"}
    missing = TestResult(
        runner="pytest",
        passed=False,
        returncode=2,
        stdout="ModuleNotFoundError: No module named 'celery'\n",
        stderr="",
        command=["pytest"],
    )
    assert classify_verify_failure(missing, packet=pkt) == "missing_dep"

    leftover = TestResult(
        runner="pytest",
        passed=False,
        returncode=1,
        stdout="tests/test_conduit_oracle.py still contains 'davinci'\n",
        stderr="",
        command=["pytest"],
    )
    assert classify_verify_failure(leftover, packet=pkt) == "leftover_token"

    none = TestResult(
        runner="none",
        passed=False,
        returncode=1,
        stdout="",
        stderr="",
        command=[],
        fail_reason="no_consumer_python: no venv under repo and no oracle tests",
        extra_notes=["verify_kind=no_consumer_python"],
    )
    assert classify_verify_failure(none, packet=pkt) == "no_consumer_python"

    ac = TestResult(
        runner="anticheat",
        passed=False,
        returncode=1,
        stdout="block",
        stderr="",
        command=[],
    )
    assert classify_verify_failure(ac, packet=pkt) == "anticheat"

    same_pkg = TestResult(
        runner="pytest",
        passed=False,
        returncode=2,
        stdout="ModuleNotFoundError: No module named 'openai'\n",
        stderr="",
        command=["pytest"],
    )
    assert classify_verify_failure(same_pkg, packet=pkt) != "missing_dep"


def test_classify_repo_local_package_is_not_missing_dep(tmp_path: Path):
    from conduit.test_runner import classify_verify_failure

    pkt = {"package": "openai"}
    (tmp_path / "web" / "reNgine").mkdir(parents=True)
    rengine_miss = TestResult(
        runner="pytest",
        passed=False,
        returncode=1,
        stdout="ModuleNotFoundError: No module named 'reNgine'\n",
        stderr="",
        command=["pytest"],
    )
    assert (
        classify_verify_failure(rengine_miss, packet=pkt, root=tmp_path)
        != "missing_dep"
    )

    leftover_and_local = TestResult(
        runner="pytest",
        passed=False,
        returncode=1,
        stdout=(
            "tests/test_conduit_oracle.py still contains 'davinci'\n"
            "ModuleNotFoundError: No module named 'reNgine'\n"
        ),
        stderr="",
        command=["pytest"],
    )
    assert (
        classify_verify_failure(leftover_and_local, packet=pkt, root=tmp_path)
        == "leftover_token"
    )

    celery = TestResult(
        runner="pytest",
        passed=False,
        returncode=2,
        stdout="ModuleNotFoundError: No module named 'celery'\n",
        stderr="",
        command=["pytest"],
    )
    assert classify_verify_failure(celery, packet=pkt, root=tmp_path) == "missing_dep"


def test_verified_tests_oracle_only_without_venv(tmp_path: Path, monkeypatch):
    from conduit.anticheat.scan import AnticheatReport
    from conduit.self_correct import _run_verified_tests

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
    )
    calls: list[list[str] | None] = []

    def fake_run_tests(root, *, timeout=300.0, nodeids=None):
        calls.append(list(nodeids) if nodeids else None)
        return TestResult(
            runner="pytest",
            passed=True,
            returncode=0,
            stdout="1 passed",
            stderr="",
            command=["pytest"],
        )

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "conduit.self_correct.run_anticheat",
        lambda *_a, **_k: AnticheatReport(),
    )
    result = _run_verified_tests(tmp_path, {"package": "openai", "rules": []})
    assert result.passed
    assert calls == [["tests/test_conduit_oracle.py"]]
    assert any("verify_mode=oracle" in n for n in result.extra_notes)


def test_verified_tests_full_suite_with_venv(tmp_path: Path, monkeypatch):
    from conduit.anticheat.scan import AnticheatReport
    from conduit.self_correct import _run_verified_tests

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
    )
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    calls: list[list[str] | None] = []

    def fake_run_tests(root, *, timeout=300.0, nodeids=None):
        calls.append(list(nodeids) if nodeids else None)
        return TestResult(
            runner="pytest",
            passed=True,
            returncode=0,
            stdout="1 passed",
            stderr="",
            command=["pytest"],
        )

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "conduit.self_correct.run_anticheat",
        lambda *_a, **_k: AnticheatReport(),
    )
    result = _run_verified_tests(tmp_path, {"package": "openai", "rules": []})
    assert result.passed
    assert calls == [["tests/test_conduit_oracle.py"], None]
    assert any("verify_mode=full" in n for n in result.extra_notes)


def test_missing_dep_skips_llm_and_post_rules(tmp_path: Path, monkeypatch):
    from conduit.self_correct import verify_with_self_correct

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_conduit_oracle.py").write_text(
        "def test_oracle():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    def fake_run_tests(root, **_kwargs):
        return TestResult(
            runner="pytest",
            passed=False,
            returncode=2,
            stdout=(
                "ERROR collecting web/tests/test_nmap.py\n"
                "ModuleNotFoundError: No module named 'celery'\n"
            ),
            stderr="",
            command=["pytest"],
        )

    synth = {"n": 0}
    llm = {"n": 0}

    def fake_synth(*_a, **_k):
        synth["n"] += 1
        return []

    class BoomClient:
        def run_agent(self, **_kwargs):
            llm["n"] += 1
            return {"files": {"celery/__init__.py": ""}}

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr(
        "conduit.patcher.post_rules.synthesize.synthesize_post_rules",
        fake_synth,
    )
    monkeypatch.setattr("conduit.self_correct.get_llm_client", lambda: BoomClient())

    logs: list[str] = []
    result, changed = verify_with_self_correct(
        tmp_path,
        {"package": "openai", "rules": []},
        max_retries=3,
        log=logs.append,
        edited_files=["app.py"],
    )
    assert not result.passed
    assert synth["n"] == 0
    assert llm["n"] == 0
    assert changed == []
    assert not (tmp_path / "celery").exists()
    assert "missing_dep" in (result.fail_reason or "")
    assert any("skipping LLM" in m for m in logs)


def test_apply_file_updates_rejects_stub_and_off_allowlist(tmp_path: Path):
    from conduit.self_correct import _apply_file_updates

    logs: list[str] = []
    changed = _apply_file_updates(
        tmp_path,
        {
            "celery/__init__.py": "app = None\n",
            "other.py": "x = 1\n",
            "app.py": "x = 2\n",
        },
        packet={"package": "openai", "rules": []},
        log=logs.append,
        path_allowlist={"app.py"},
    )
    assert changed == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert not (tmp_path / "celery").exists()
    assert not (tmp_path / "other.py").exists()
    assert any("celery" in m for m in logs)
    assert any("allowlist" in m for m in logs)

    stub_only = _apply_file_updates(
        tmp_path,
        {"celery/__init__.py": "app = None\n"},
        packet={"package": "openai", "rules": []},
        log=logs.append,
        path_allowlist={"celery/__init__.py"},
    )
    assert stub_only == []
    assert not (tmp_path / "celery").exists()
