"""Conduit unit tests (offline)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from conduit.detect.lockfile_diff import detect_lockfile_jumps, diff_versions
from conduit.detect.modules.discovery import load_modules
from conduit.export_delta.diff import diff_exports
from conduit.export_delta.extract import _python_file_exports
from conduit.llm.client import resolve_provider
from conduit.detect.models import ChangeSignal
from conduit.detect.client_state import PackageClientState
from conduit.packet.synthesize import (
    collapse_dependency_bumps,
    ensure_packet,
    filter_signals_to_source,
    load_fixture_openai_packet,
)
from conduit.packet.validate import validate_packet
from conduit.main import _resolve_packet_arg, app
from conduit.patcher import apply_packet
from conduit.patcher.ast_attr_call import rename_python_attr, rewrite_python_call
from conduit.patcher.ast_import_rewrite import rewrite_python_imports
from conduit.patcher.key_rename import apply_key_rename
from conduit.prune.grep_imports import prune_by_imports
from conduit.scaffold.module_new import scaffold_module
from conduit.scaffold.packet_init import scaffold_packet
from conduit.test_gen import (
    ensure_tests,
    oracle_forbidden_tokens,
    oracle_scan_rels,
    token_in_text,
)

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "examples" / "demo-consumer"
SAMPLE_PACKET = REPO / "examples" / "sample-packet" / "conduit-packet.json"


def test_diff_versions_requirements():
    old = "openai==0.28.1\nrequests==2.0.0\n"
    new = "openai==1.0.0\nrequests==2.0.0\n"
    jumps = diff_versions(old, new, filename="requirements.txt")
    assert len(jumps) == 1
    assert jumps[0].name == "openai"
    assert jumps[0].is_major


def test_detect_lockfile_jumps_missing_root(tmp_path: Path):
    """Invalid cwd must not raise (Windows WinError 267)."""
    missing = tmp_path / "does-not-exist"
    assert detect_lockfile_jumps(missing) == []


def test_detect_lockfile_jumps_non_git_dir(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "requirements.txt").write_text("openai==1.0.0\n", encoding="utf-8")
    assert detect_lockfile_jumps(plain) == []


def test_export_delta_placeholder_version(tmp_path: Path):
    from conduit.export_delta import compute_export_delta

    delta = compute_export_delta(
        package="openai",
        from_version="0.0.0",
        to_version="1.0.0",
        ecosystem="pypi",
        cache_root=tmp_path / "exports",
    )
    assert delta.skipped_reason is not None
    assert "openai==0.0.0" in delta.skipped_reason
    assert delta.diagnostics
    assert "placeholder" in delta.diagnostics[0]


def test_resolve_packet_arg_package_name():
    path, pkg = _resolve_packet_arg("openai")
    assert path is None
    assert pkg == "openai"


def test_resolve_packet_arg_file(tmp_path: Path):
    packet_file = tmp_path / "conduit-packet.json"
    packet_file.write_text("{}", encoding="utf-8")
    path, pkg = _resolve_packet_arg(str(packet_file))
    assert path == packet_file.resolve()
    assert pkg is None


def test_ensure_packet_prefers_source_installed_over_signal(tmp_path: Path):
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="SDK_MAJOR_BUMP",
            from_version="1.109.1",
            to_version="0.28.1",
            suggested_rules=[
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "from_version": "1.109.1",
                    "to_version": "0.28.1",
                    "ecosystems": ["pip"],
                }
            ],
        ),
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="SDK_MAJOR_BUMP",
            from_version="0.28.1",
            to_version="1.109.1",
            suggested_rules=[
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "from_version": "0.28.1",
                    "to_version": "1.109.1",
                    "ecosystems": ["pip"],
                }
            ],
        ),
    ]
    state = PackageClientState(
        package="openai",
        installed_version="0.28.1",
        model_ids=["text-davinci-edit-001"],
    )
    result = ensure_packet(
        tmp_path,
        signals,
        package="openai",
        installed={"openai": "0.28.1"},
        use_fixture_fallback=False,
        client_state=state,
    )
    assert result.packet["from_version"] == "0.28.1"
    assert result.from_source == "source"
    assert result.packet["to_version"] == "1.109.1"
    bumps = [r for r in result.packet["rules"] if r.get("type") == "DEPENDENCY_BUMP"]
    assert len(bumps) == 1
    assert bumps[0]["from_version"] == "0.28.1"
    assert bumps[0]["to_version"] == "1.109.1"


def test_filter_signals_drops_unused_catalog_models():
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="gpt-realtime",
            replacement_pattern="gpt-realtime-2.1",
        ),
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            affected_pattern="text-davinci-edit-001",
            replacement_pattern="gpt-4o",
        ),
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="SDK_MAJOR_BUMP",
            from_version="0.28.1",
            to_version="1.109.1",
        ),
    ]
    source = {
        "model_ids": ["text-davinci-edit-001"],
        "api_patterns": ["Edit.create"],
        "usages": [
            {
                "id": "text-davinci-edit-001",
                "callees": ["Edit.create"],
                "paths": ["/v1/edits"],
                "files": ["edits.py"],
            }
        ],
    }
    scoped = filter_signals_to_source(signals, source, package="openai")
    aff = {s.affected_pattern for s in scoped}
    assert "text-davinci-edit-001" in aff
    assert "gpt-realtime" not in aff
    assert any(s.change_type == "SDK_MAJOR_BUMP" for s in scoped)


def test_collapse_dependency_bumps_keeps_one():
    rules = collapse_dependency_bumps(
        [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "1.109.1",
                "to_version": "0.28.1",
            },
            {"type": "EXACT_STRING_REPLACE", "match": "a", "replace": "b"},
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "1.109.1",
            },
        ],
        package="openai",
        from_version="0.28.1",
        to_version="1.109.1",
    )
    bumps = [r for r in rules if r["type"] == "DEPENDENCY_BUMP"]
    assert len(bumps) == 1
    assert bumps[0]["from_version"] == "0.28.1"
    assert bumps[0]["to_version"] == "1.109.1"


def test_ensure_packet_uses_manifest_from_version(tmp_path: Path):
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="API_BREAKING",
            suggested_rules=[
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "from_version": "0.28.1",
                    "to_version": "1.40.0",
                    "ecosystems": ["pip"],
                }
            ],
        )
    ]
    result = ensure_packet(
        tmp_path,
        signals,
        package="openai",
        installed={"openai": "0.28.1"},
        use_fixture_fallback=False,
    )
    assert result.packet["from_version"] == "0.28.1"
    assert result.from_source == "manifest"
    assert result.packet["to_version"] == "1.40.0"
    assert result.to_source == "rule"
    assert not any("defaulted" in w for w in result.warnings)


def test_ensure_packet_warns_on_placeholder_versions(tmp_path: Path):
    signals = [
        ChangeSignal(
            source="module:acme",
            package="acme",
            change_type="API_BREAKING",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "match": "old",
                    "replace": "new",
                    "target_files": ["*.py"],
                }
            ],
        )
    ]
    result = ensure_packet(
        tmp_path,
        signals,
        package="acme",
        installed={},
        use_fixture_fallback=False,
    )
    assert result.from_source == "placeholder"
    assert result.to_source == "placeholder"
    assert any("from_version defaulted" in w for w in result.warnings)
    assert any("to_version defaulted" in w for w in result.warnings)


def test_load_modules_includes_openai():
    names = {m.name for m in load_modules()}
    assert "openai" in names


def test_fixture_packet_valid():
    packet = load_fixture_openai_packet()
    assert validate_packet(packet) == []


def test_sample_packet_valid():
    data = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    assert validate_packet(data) == []


def test_prune_demo_imports_openai():
    files = prune_by_imports(DEMO, ["openai"])
    rels = {str(p.relative_to(DEMO)).replace("\\", "/") for p in files}
    assert any("ai_client.py" in r for r in rels)


def test_apply_packet_nets_opposing_bumps(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("openai==1.109.1\n", encoding="utf-8")
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.109.1",
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "1.109.1",
                "to_version": "0.28.1",
                "ecosystems": ["pip"],
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "1.109.1",
                "ecosystems": ["pip"],
            },
        ],
    }
    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    text = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert "openai==1.109.1" in text
    assert "0.28.1" not in text
    bump_changes = [c for c in report.changes if c.rule_type == "DEPENDENCY_BUMP"]
    assert len(bump_changes) == 1
    assert "1.109.1" in bump_changes[0].detail
    assert bump_changes[0].detail.count("->") == 1


def test_apply_packet_dry_run_demo(tmp_path: Path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    packet = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    files = prune_by_imports(dest, ["openai"])
    report = apply_packet(dest, packet, dry_run=True, file_allowlist=files or None)
    assert report.changes or report.files_modified is not None
    text = (dest / "src" / "ai_client.py").read_text(encoding="utf-8")
    assert "gpt-4-0613" in text


def test_apply_packet_writes_demo(tmp_path: Path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    packet = json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))
    files = prune_by_imports(dest, ["openai"])
    report = apply_packet(dest, packet, dry_run=False, file_allowlist=files or None)
    text = (dest / "src" / "ai_client.py").read_text(encoding="utf-8")
    assert "gpt-4o" in text
    assert "max_completion_tokens" in text
    assert report.files_modified


def test_scaffold_packet(tmp_path: Path):
    path = scaffold_packet(
        package="stripe",
        ecosystem="pypi",
        from_version="1.0.0",
        to_version="2.0.0",
        out_dir=tmp_path / "stripe-packet",
    )
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["package"] == "stripe"
    assert validate_packet(data) == []


def test_scaffold_module(tmp_path: Path):
    (tmp_path / "src" / "conduit" / "detect" / "modules").mkdir(parents=True)
    mod_dir = scaffold_module("acme", package="acme", target_root=tmp_path)
    assert (mod_dir / "__init__.py").is_file()


def test_resolve_provider_openai(monkeypatch):
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_provider() == "openai"


def test_resolve_provider_anthropic(monkeypatch):
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert resolve_provider() == "anthropic"


def test_resolve_provider_explicit_ollama(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "ollama")
    assert resolve_provider() == "ollama"


def test_resolve_provider_explicit_custom(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "custom")
    assert resolve_provider() == "custom"


def test_resolve_provider_legacy_alias(monkeypatch):
    monkeypatch.setenv("CONDUIT_LLM_PROVIDER", "openai_compatible")
    assert resolve_provider() == "custom"


def test_diff_exports_rename_case():
    added, removed, renamed = diff_exports({"FooBar"}, {"foobar"})
    assert renamed.get("FooBar") == "foobar"
    assert not added
    assert not removed


def test_python_file_exports_all():
    src = '__all__ = ["Client", "util"]\n\ndef helper():\n    pass\n'
    syms = _python_file_exports(src)
    assert "Client" in syms
    assert "util" in syms
    assert "helper" in syms


def test_rewrite_python_imports():
    src = "from openai import OpenAI\n"
    out, n = rewrite_python_imports(src, "openai", "openai_v1")
    assert n >= 1
    assert "openai_v1" in out


def test_rename_python_attr_and_call():
    src = "x = openai.ChatCompletion.create()\n"
    out, n = rename_python_attr(src, "openai.ChatCompletion", "openai.chat.completions")
    assert n >= 1
    assert "chat.completions" in out
    src2 = "openai.ChatCompletion.create()\n"
    out2, n2 = rewrite_python_call(
        src2, "openai.ChatCompletion.create", "openai.chat.completions.create"
    )
    assert n2 >= 1


def _disable_llm(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CONDUIT_LLM_BASE_URL", raising=False)


def test_ensure_tests_empty_rules_import_smoke_no_tautology(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    packet = {
        "package": "demo",
        "ecosystem": "pypi",
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "rules": [],
    }
    created = ensure_tests(tmp_path, packet)
    assert created == ["tests/test_conduit_oracle.py"]
    text = (tmp_path / created[0]).read_text(encoding="utf-8")
    assert "or True" not in text
    assert "test_package_importable" in text


def test_ensure_tests_oracle_fails_on_leftover_match(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    app = tmp_path / "app.py"
    app.write_text("model = 'gpt-4-0613'\n", encoding="utf-8")
    packet = {
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }
    created = ensure_tests(tmp_path, packet, file_allowlist=[app])
    assert created == ["tests/test_conduit_oracle.py"]
    assert token_in_text(app.read_text(encoding="utf-8"), "gpt-4-0613")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / created[0]), "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "gpt-4-0613" in (proc.stdout + proc.stderr)


def test_ensure_tests_oracle_skips_ignored_contract(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text("model = 'gpt-4o'\n", encoding="utf-8")
    oracle = tmp_path / "policy.py"
    oracle.write_text(
        "LEGACY_MODEL = 'gpt-4-0613'\n",
        encoding="utf-8",
    )
    packet = {
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "ignore": {"paths": ["policy.py"]},
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }
    created = ensure_tests(
        tmp_path,
        packet,
        file_allowlist=[tmp_path / "app.py", oracle],
    )
    text = (tmp_path / created[0]).read_text(encoding="utf-8")
    assert "app.py" in text
    assert "policy.py" not in text


def test_ensure_tests_writes_oracle_when_native_suite_exists(
    tmp_path: Path, monkeypatch
):
    _disable_llm(monkeypatch)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    app = tmp_path / "app.py"
    app.write_text("x = 1\n", encoding="utf-8")
    packet = {
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "1",
        "to_version": "2",
        "rules": [
            {
                "type": "AST_PARAM_RENAME",
                "target_files": ["*.py"],
                "function_target": "create",
                "old_param": "max_tokens",
                "new_param": "max_completion_tokens",
            }
        ],
    }
    created = ensure_tests(tmp_path, packet, file_allowlist=[app])
    assert "tests/test_conduit_oracle.py" in created
    text = (tmp_path / "tests" / "test_conduit_oracle.py").read_text(encoding="utf-8")
    assert "max_tokens" in text
    assert "test_conduit_no_legacy_tokens" in text


def test_ensure_tests_writes_js_oracle(tmp_path: Path, monkeypatch):
    _disable_llm(monkeypatch)
    app = tmp_path / "index.js"
    app.write_text("const model = 'gpt-4-0613';\n", encoding="utf-8")
    packet = {
        "package": "openai",
        "ecosystem": "npm",
        "from_version": "3",
        "to_version": "4",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.js"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }
    created = ensure_tests(tmp_path, packet, file_allowlist=[app])
    assert created == ["conduit_oracle.test.js"]
    text = (tmp_path / created[0]).read_text(encoding="utf-8")
    assert "gpt-4-0613" in text
    assert "conduit no legacy tokens" in text
    assert "or True" not in text


def _leftover_packet() -> dict:
    return {
        "packet_id": "openai-0.28.1-1.0.0",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0.28.1",
        "to_version": "1.0.0",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "target_files": ["*.py"],
                "match": "gpt-4-0613",
                "replace": "gpt-4o",
            }
        ],
    }


def test_verify_cmd_writes_oracle(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    pkt = tmp_path / "conduit-packet.json"
    pkt.write_text(json.dumps(_leftover_packet()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "verify",
            "--path",
            str(tmp_path),
            "--packet",
            str(pkt),
            "--max-retries",
            "1",
        ],
    )
    oracle = tmp_path / "tests" / "test_conduit_oracle.py"
    assert oracle.is_file(), result.output
    text = oracle.read_text(encoding="utf-8")
    assert "test_conduit_no_legacy_tokens" in text
    assert "leftover-token oracle" in text


def test_apply_cmd_does_not_write_oracle(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    _disable_llm(monkeypatch)
    (tmp_path / "app.py").write_text(
        "import openai\nmodel = 'gpt-4-0613'\n", encoding="utf-8"
    )
    pkt = tmp_path / "conduit-packet.json"
    pkt.write_text(json.dumps(_leftover_packet()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["apply", "--path", str(tmp_path), "--packet", str(pkt)],
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "tests" / "test_conduit_oracle.py").exists()


def test_attr_rename_rule_in_packet(tmp_path: Path):
    src = tmp_path / "mod.py"
    src.write_text("import openai\nx = openai.ChatCompletion\n", encoding="utf-8")
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [
            {
                "type": "AST_ATTR_RENAME",
                "target_files": ["*.py"],
                "old_attr": "openai.ChatCompletion",
                "new_attr": "openai.chat.completions",
            }
        ],
    }
    assert validate_packet(packet) == []
    report = apply_packet(tmp_path, packet, dry_run=False, require_context=False)
    text = src.read_text(encoding="utf-8")
    assert "chat.completions" in text
    assert report.files_modified

def test_self_correct_verbose_logs_failure_and_fix(tmp_path: Path, monkeypatch):
    from conduit.self_correct import verify_with_self_correct
    from conduit.test_runner import TestResult

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("max_tokens = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")

    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "max_tokens",
                "replace": "max_completion_tokens",
                "target_files": ["*.py"],
            }
        ],
    }

    calls = {"n": 0}

    def fake_run_tests(root):
        calls["n"] += 1
        if calls["n"] == 1:
            return TestResult(
                runner="pytest",
                passed=False,
                returncode=1,
                stdout="FAILED tests/test_app.py::test_ok - assert False",
                stderr="",
                command=["pytest", "-q"],
            )
        return TestResult(
            runner="pytest",
            passed=True,
            returncode=0,
            stdout="",
            stderr="",
            command=["pytest", "-q"],
        )

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr("conduit.self_correct.get_llm_client", lambda: None)

    logs: list[str] = []
    result, corrected = verify_with_self_correct(
        tmp_path,
        packet,
        max_retries=2,
        verbose=True,
        log=logs.append,
    )
    assert result.passed
    assert any("src" in c.replace("\\\\", "/") for c in corrected) or any(
        "app.py" in c for c in corrected
    )
    joined = "\n".join(logs)
    assert "failure summary" in joined
    assert "assert False" in joined
    assert "strategy=heuristic" in joined
    assert "max_tokens" in joined
    assert "max_completion_tokens" in joined

def test_self_correct_stops_early_when_heuristic_noop(tmp_path: Path, monkeypatch):
    from conduit.self_correct import verify_with_self_correct
    from conduit.test_runner import TestResult

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("max_completion_tokens = 1\n", encoding="utf-8")

    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [
            {
                "type": "EXACT_STRING_REPLACE",
                "match": "max_tokens",
                "replace": "max_completion_tokens",
                "target_files": ["*.py"],
            }
        ],
    }

    calls = {"n": 0}

    def fake_run_tests(root):
        calls["n"] += 1
        return TestResult(
            runner="pytest",
            passed=False,
            returncode=1,
            stdout="FAILED",
            stderr="",
            command=["pytest", "-q"],
        )

    monkeypatch.setattr("conduit.self_correct.run_tests", fake_run_tests)
    monkeypatch.setattr("conduit.self_correct.get_llm_client", lambda: None)

    logs: list[str] = []
    result, corrected = verify_with_self_correct(
        tmp_path,
        packet,
        max_retries=5,
        verbose=True,
        log=logs.append,
    )
    assert not result.passed
    assert corrected == []
    assert calls["n"] == 1  # initial failure only; no empty retries
    joined = "\n".join(logs)
    assert "stopping early" in joined
    assert "no remaining occurrences of ''max_tokens''" in joined or "no remaining occurrences of 'max_tokens'" in joined


def test_ensure_packet_refresh_skips_cache(tmp_path: Path):
    signals = [
        ChangeSignal(
            source="module:openai",
            package="openai",
            change_type="MODEL_DEPRECATION",
            suggested_rules=[
                {
                    "type": "EXACT_STRING_REPLACE",
                    "match": "old-model",
                    "replace": "new-model",
                    "target_files": ["*.py"],
                }
            ],
        )
    ]
    first = ensure_packet(
        tmp_path,
        signals,
        package="openai",
        installed={"openai": "1.0.0"},
        use_fixture_fallback=False,
    )
    assert first.packet["from_version"] == "1.0.0"
    # Poison the cache
    from conduit.packet.cache import cache_path

    path = cache_path(tmp_path, "openai", "1.0.0", "1.0.0")
    stale = dict(first.packet)
    stale["notes"] = "STALE"
    path.write_text(json.dumps(stale), encoding="utf-8")

    cached = ensure_packet(
        tmp_path,
        signals,
        package="openai",
        installed={"openai": "1.0.0"},
        use_fixture_fallback=False,
    )
    assert cached.packet.get("notes") == "STALE"

    refreshed = ensure_packet(
        tmp_path,
        signals,
        package="openai",
        installed={"openai": "1.0.0"},
        use_fixture_fallback=False,
        refresh=True,
    )
    assert refreshed.packet.get("notes") != "STALE"
    assert any("refreshed packet cache" in w for w in refreshed.warnings)


def test_exact_replace_skips_identifier_tokens():
    from conduit.patcher.string_replace import exact_replace

    src = "def test_uses_text_davinci_003():\n    m = 'text-davinci-003'\n"
    # Short legacy id must not rewrite the function name
    out, n = exact_replace(src, "davinci", "gpt-3.5-turbo")
    assert n == 0
    assert "def test_uses_text_davinci_003" in out
    # Full model id in a string still replaces
    out2, n2 = exact_replace(src, "text-davinci-003", "gpt-5.6-terra")
    assert n2 == 1
    assert "gpt-5.6-terra" in out2
    assert "def test_uses_text_davinci_003" in out2


def test_exact_replace_skips_model_id_prefixes():
    from conduit.patcher.string_replace import exact_replace

    src = 'model = "gpt-4-0613"\n'
    out, n = exact_replace(src, "gpt-4", "gpt-5.6-sol")
    assert n == 0
    assert "gpt-4-0613" in out
    out2, n2 = exact_replace(src, "gpt-4-0613", "gpt-5.6-sol")
    assert n2 == 1
    assert out2 == 'model = "gpt-5.6-sol"\n'

    from conduit.detect.modules.openai.workers.deprecation_scraper import parse_deprecation_html

    html = """
    <table>
      <tr><th>Shutdown date</th><th>Model / system</th><th>Recommended replacement</th></tr>
      <tr><td>Oct 23, 2026</td><td>gpt-4-0613</td><td>gpt-5.6-sol</td></tr>
      <tr><td>2026-05-12</td><td>dall-e-2</td><td>gpt-image-2 , gpt-image-1</td></tr>
      <tr><td>2022-12-03</td><td>/v1/engines</td><td>/v1/models</td></tr>
      <tr><td>2026-09-24</td><td>Videos API</td><td>---</td></tr>
    </table>
    """
    signals = parse_deprecation_html(html, "https://example.test/deprecations")
    by = {s.affected_pattern: s for s in signals}
    assert by["gpt-4-0613"].replacement_pattern == "gpt-5.6-sol"
    assert by["dall-e-2"].replacement_pattern == "gpt-image-2"
    assert by["/v1/engines"].replacement_pattern == "/v1/models"
    assert "Videos API" not in by


def test_parse_fixture_deprecation_still_works():
    from conduit.detect.modules.openai.models_legacy import ChangeType
    from conduit.detect.modules.openai.workers.deprecation_scraper import DeprecationScraperWorker

    signals = DeprecationScraperWorker().run(demo=True)
    assert len(signals) == 5
    assert any(s.affected_pattern == "gpt-4-0613" for s in signals)
    assert any(s.affected_pattern == "davinci" for s in signals)
    assert any(
        s.change_type == ChangeType.API_BREAKING
        and s.affected_pattern == "/v1/completions"
        and s.replacement_pattern == "/v1/chat/completions"
        for s in signals
    )


def test_openapi_load_sniffs_yaml_without_suffix(tmp_path: Path):
    from conduit.detect.modules.openai.workers.openapi_diff import _load_openapi

    path = tmp_path / "previous-openapi"
    path.write_text("openapi: 3.0.0\npaths: {}\n", encoding="utf-8")
    data = _load_openapi(path)
    assert data["openapi"] == "3.0.0"


def test_exact_replace_skips_identifier_tokens():
    from conduit.patcher.string_replace import exact_replace

    src = "def test_uses_text_davinci_003():\n    m = 'text-davinci-003'\n"
    out, n = exact_replace(src, "davinci", "gpt-3.5-turbo")
    assert n == 0
    assert "def test_uses_text_davinci_003" in out
    out2, n2 = exact_replace(src, "text-davinci-003", "gpt-5.6-terra")
    assert n2 == 1
    assert "gpt-5.6-terra" in out2
    assert "def test_uses_text_davinci_003" in out2


def test_exact_replace_skips_model_id_prefixes():
    from conduit.patcher.string_replace import exact_replace

    src = 'model = "gpt-4-0613"\n'
    out, n = exact_replace(src, "gpt-4", "gpt-5.6-sol")
    assert n == 0
    assert "gpt-4-0613" in out
    out2, n2 = exact_replace(src, "gpt-4-0613", "gpt-5.6-sol")
    assert n2 == 1
    assert out2 == 'model = "gpt-5.6-sol"\n'


def _key_rename_packet(**extra) -> dict:
    packet = {
        "packet_id": "t",
        "package": "openai",
        "ecosystem": "pypi",
        "from_version": "0",
        "to_version": "1",
        "rules": [
            {
                "type": "KEY_RENAME",
                "old_key": "max_tokens",
                "new_key": "max_completion_tokens",
                "target_files": [
                    "*.py",
                    "*.json",
                    "*.yaml",
                    "*.yml",
                    "*.toml",
                    ".env*",
                ],
            }
        ],
    }
    packet.update(extra)
    return packet


def test_apply_key_rename_quoted_and_env():
    py, n_py = apply_key_rename(
        'data["max_tokens"] = payload["max_tokens"]\n',
        "max_tokens",
        "max_completion_tokens",
    )
    assert n_py == 2
    assert "max_tokens" not in py
    assert 'data["max_completion_tokens"]' in py

    yaml, n_yaml = apply_key_rename(
        '"max_tokens": 128\n',
        "max_tokens",
        "max_completion_tokens",
    )
    assert n_yaml == 1
    assert yaml == '"max_completion_tokens": 128\n'

    env, n_env = apply_key_rename(
        "MAX_TOKENS=128\nexport max_tokens=64\n",
        "max_tokens",
        "max_completion_tokens",
        env_file=True,
    )
    assert n_env >= 2
    assert "MAX_COMPLETION_TOKENS=128" in env
    assert "export max_completion_tokens=64" in env
    assert "MAX_TOKENS=" not in env


def test_key_rename_packet_validates_with_side_effects():
    packet = _key_rename_packet(
        side_effects=[
            {
                "kind": "webhook",
                "detail": "Receivers must accept max_completion_tokens.",
            },
            {
                "kind": "database",
                "detail": "Migrate stored completion param name if persisted.",
            },
        ]
    )
    assert validate_packet(packet) == []


def test_key_rename_updates_py_yaml_env_despite_prune(tmp_path: Path):
    src = tmp_path / "app.py"
    src.write_text(
        'import openai\npayload = {"max_tokens": 10}\n',
        encoding="utf-8",
    )
    yaml = tmp_path / "config.yaml"
    yaml.write_text('"max_tokens": 128\n', encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("MAX_TOKENS=128\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("print('no vendor')\n", encoding="utf-8")

    packet = _key_rename_packet()
    report = apply_packet(
        tmp_path,
        packet,
        dry_run=False,
        require_context=True,
        file_allowlist=[src],
    )
    assert '"max_completion_tokens": 10' in src.read_text(encoding="utf-8")
    assert yaml.read_text(encoding="utf-8") == '"max_completion_tokens": 128\n'
    assert env.read_text(encoding="utf-8") == "MAX_COMPLETION_TOKENS=128\n"
    assert "config.yaml" in report.files_modified
    assert ".env" in report.files_modified


def test_oracle_key_rename_scans_config_and_forbids_old_key(tmp_path: Path):
    app = tmp_path / "app.py"
    app.write_text("import openai\n", encoding="utf-8")
    yaml = tmp_path / "settings.yaml"
    yaml.write_text('"max_tokens": 1\n', encoding="utf-8")
    env = tmp_path / ".env.local"
    env.write_text("MAX_TOKENS=1\n", encoding="utf-8")
    packet = _key_rename_packet()
    tokens = oracle_forbidden_tokens(packet)
    assert "max_tokens" in tokens
    rels = oracle_scan_rels(tmp_path, packet, file_allowlist=[app])
    assert "settings.yaml" in rels
    assert ".env.local" in rels

