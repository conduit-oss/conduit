"""Tests for post-apply pip sync of DEPENDENCY_BUMP pins."""

from __future__ import annotations

import pytest

from conduit.patcher.sync_env import (
    VerifyEnvError,
    bumped_package_specs,
    explain_dep_conflict,
    format_pip_failure,
    format_verify_env_failure,
    is_dep_conflict,
    sync_bumped_packages,
)


def test_bumped_package_specs_dedupes_and_skips_npm_only():
    packet = {
        "rules": [
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "3.3.1",
                "ecosystems": ["pip", "pyproject"],
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "0.28.1",
                "to_version": "3.3.1",
                "ecosystems": ["pip"],
            },
            {
                "type": "DEPENDENCY_BUMP",
                "package": "openai",
                "from_version": "3.0.0",
                "to_version": "4.0.0",
                "ecosystems": ["npm"],
            },
        ]
    }
    assert bumped_package_specs(packet) == ["openai==3.3.1"]


def test_format_pip_failure_prefers_conflict_over_pip_notice():
    blob = """
Collecting litellm==1.96.2
ERROR: Cannot install -r requirements.txt (line 19) and openai==3.3.1 because these package versions have conflicting dependencies.
The conflict is caused by:
    The user requested openai==3.3.1
    litellm 1.96.2 depends on openai<3.0.0 and >=2.20.0
ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/...
[notice] A new release of pip is available: 23.0.1 -> 26.2.1
[notice] To update, run: python.exe -m pip install --upgrade pip
"""
    text = format_pip_failure(blob)
    assert "conflicting dependencies" in text
    assert "litellm 1.96.2 depends on openai" in text
    assert "Collecting litellm" not in text
    assert "A new release of pip" not in text
    assert "for help visit" not in text
    assert is_dep_conflict(text)


def test_format_pip_failure_drops_collecting_noise_keeps_error():
    blob = """
Collecting jaraco-context==6.1.2
  Using cached jaraco_context-6.1.2-py3-none-any.whl (7.9 kB)
Collecting openai==3.3.1
  Downloading openai-3.3.1-py3-none-any.whl (1.7 MB)
     ---------------------------------------- 1.7/1.7 MB 22.7 MB/s  0:00:00
ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied
Ignoring google-cloud-storage: markers 'python_version >= "3.13"' don't match your environment
[notice] A new release of pip is available
"""
    text = format_pip_failure(blob)
    assert "Access is denied" in text
    assert "Collecting openai" not in text
    assert "Downloading openai" not in text
    assert "Ignoring google-cloud-storage" not in text


def test_format_verify_env_failure_explains_hash_mismatch():
    detail = (
        "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
        "    openai==3.3.1 from https://files.pythonhosted.org/.../openai-3.3.1-py3-none-any.whl:\n"
        "        Expected sha256 e0f388ce499f53f58079d0c1f571f356f2b168b84d0d24a412506b6abc714980\n"
        "             Got        9652df7fdf8ee6f5bd58e0a12f2b1d414a18e0f06bb7a9a57c8643a5f5469bd3\n"
    )
    text = format_verify_env_failure(detail, specs=["openai==3.3.1"])
    assert "stale or missing --hash=" in text.lower()
    assert "openai==3.3.1" in text
    assert "DO NOT MATCH THE HASHES" in text


def test_explain_dep_conflict_names_blocker_and_pin():
    detail = (
        "ERROR: Cannot install -r requirements.txt (line 19) and openai==3.3.1 "
        "because these package versions have conflicting dependencies.\n"
        "The conflict is caused by:\n"
        "    The user requested openai==3.3.1\n"
        "    litellm 1.96.2 depends on openai<3.0.0 and >=2.20.0\n"
        "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/\n"
    )
    text = explain_dep_conflict(detail, specs=["openai==3.3.1"])
    assert "Migrated pin: openai==3.3.1" in text
    assert "Blocked by: litellm 1.96.2 (requires openai<3.0.0 and >=2.20.0)" in text
    assert "ResolutionImpossible" not in text
    assert "for help visit" not in text

    wrapped = format_verify_env_failure(detail, specs=["openai==3.3.1"])
    assert wrapped.startswith("Dependency conflict")
    assert "Blocked by: litellm" in wrapped


def test_explain_dep_conflict_resolves_requirements_line(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "aiohttp==3.9.0\n"
        "litellm==1.96.2\n"
        "openai==3.3.1\n",
        encoding="utf-8",
    )
    detail = (
        f"ERROR: Cannot install -r {req} (line 2) and openai==3.3.1 "
        "because these package versions have conflicting\n"
        "dependencies.\n"
    )
    text = explain_dep_conflict(detail, specs=["openai==3.3.1"])
    assert "Migrated pin: openai==3.3.1" in text
    assert "Blocked by requirements.txt line 2: litellm==1.96.2" in text
    assert "could not parse blocker" not in text.lower()
    assert "Bump or relax the blocker" in text


def test_detect_test_command_uses_sys_executable(tmp_path):
    import sys

    from conduit.test_runner import detect_test_command

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    runner, cmd = detect_test_command(tmp_path)
    assert runner == "pytest"
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "pytest"]


def test_rewrite_shell_argv_pins_python_and_pip():
    import sys

    from conduit.llm.executors import rewrite_shell_argv

    assert rewrite_shell_argv(["python", "-c", "import openai"])[0] == sys.executable
    assert rewrite_shell_argv(["pip", "show", "openai"])[:4] == [
        sys.executable,
        "-m",
        "pip",
        "show",
    ]
    assert rewrite_shell_argv(["pytest", "-q"])[:3] == [
        sys.executable,
        "-m",
        "pytest",
    ]


def test_sync_bumped_packages_invokes_pip(monkeypatch):
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr("conduit.patcher.sync_env.subprocess.run", _run)
    logs: list[str] = []
    specs = sync_bumped_packages(
        {
            "rules": [
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "to_version": "3.3.1",
                    "ecosystems": ["pip"],
                }
            ]
        },
        python="python",
        log=logs.append,
    )
    assert specs == ["openai==3.3.1"]
    assert calls and calls[0][:4] == ["python", "-m", "pip", "install"]
    assert "openai==3.3.1" in calls[0]
    assert "pytest" in calls[0]
    assert any("Installing bumped packages" in line for line in logs)
    assert any(c[1:3] == ["-c", "import openai"] for c in calls)
    assert any(c[1:4] == ["-m", "pytest", "--version"] for c in calls)


def test_sync_installs_consumer_requirements(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc()

    (tmp_path / "requirements.txt").write_text(
        "openai==0.28.1\npython-dotenv==1.0.1\n", encoding="utf-8"
    )
    venv_py = tmp_path / ".conduit" / "verify-venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    monkeypatch.setattr("conduit.patcher.sync_env.subprocess.run", _run)
    monkeypatch.setattr(
        "conduit.test_runner.ensure_consumer_python",
        lambda root, *, log=None: str(venv_py),
    )
    monkeypatch.setattr(
        "conduit.test_runner.interpreter_belongs_to_root",
        lambda python, root: True,
    )

    logs: list[str] = []
    sync_bumped_packages(
        {
            "rules": [
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "to_version": "3.3.1",
                    "ecosystems": ["pip"],
                }
            ]
        },
        root=tmp_path,
        log=logs.append,
    )
    req_calls = [c for c in calls if "-r" in c]
    bump_calls = [c for c in calls if "openai==3.3.1" in c]
    assert req_calls, "expected pip install -r requirements.txt"
    assert any(
        str(tmp_path / "requirements.txt") in c or "requirements.txt" in " ".join(c)
        for c in req_calls
    )
    assert bump_calls, "expected openai pin after requirements"
    assert calls.index(req_calls[0]) < calls.index(bump_calls[0])
    assert any("consumer requirements" in line.lower() for line in logs)


def test_sync_aborts_early_on_requirements_dep_conflict(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "openai==3.3.1\nlitellm==1.96.2\n", encoding="utf-8"
    )
    venv_py = tmp_path / ".conduit" / "verify-venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    conflict = (
        "ERROR: Cannot install -r requirements.txt (line 2) and openai==3.3.1 "
        "because these package versions have conflicting dependencies.\n"
        "The conflict is caused by:\n"
        "    The user requested openai==3.3.1\n"
        "    litellm 1.96.2 depends on openai<3.0.0 and >=2.20.0\n"
        "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/\n"
        "[notice] A new release of pip is available: 23.0.1 -> 26.2.1\n"
    )

    class _Proc:
        def __init__(self, code=0, stderr=""):
            self.returncode = code
            self.stdout = ""
            self.stderr = stderr

    def _run(cmd, **kwargs):
        if "-r" in cmd:
            return _Proc(1, stderr=conflict)
        return _Proc(0)

    monkeypatch.setattr("conduit.patcher.sync_env.subprocess.run", _run)
    monkeypatch.setattr(
        "conduit.test_runner.ensure_consumer_python",
        lambda root, *, log=None: str(venv_py),
    )
    monkeypatch.setattr(
        "conduit.test_runner.interpreter_belongs_to_root",
        lambda python, root: True,
    )

    logs: list[str] = []
    with pytest.raises(VerifyEnvError) as excinfo:
        sync_bumped_packages(
            {
                "rules": [
                    {
                        "type": "DEPENDENCY_BUMP",
                        "package": "openai",
                        "to_version": "3.3.1",
                        "ecosystems": ["pip"],
                    }
                ]
            },
            root=tmp_path,
            log=logs.append,
        )
    msg = str(excinfo.value)
    assert "Dependency conflict" in msg
    assert "Migrated pin: openai==3.3.1" in msg
    assert "Blocked by: litellm 1.96.2" in msg
    assert "openai<3.0.0" in msg
    assert "for help visit" not in msg
    assert any("litellm" in line.lower() for line in logs)
    assert not any("Installing bumped packages" in line for line in logs)


def test_sync_bumped_packages_repairs_corrupt_venv(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, code=0, stderr=""):
            self.returncode = code
            self.stdout = "ok"
            self.stderr = stderr

    venv_py = tmp_path / ".conduit" / "verify-venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    recreated: list[str] = []

    def _recreate(root, *, log=None):
        recreated.append(str(root))
        return str(venv_py)

    monkeypatch.setattr("conduit.test_runner.recreate_verify_venv", _recreate)
    monkeypatch.setattr(
        "conduit.test_runner.ensure_consumer_python",
        lambda root, *, log=None: str(venv_py),
    )
    monkeypatch.setattr(
        "conduit.test_runner.interpreter_belongs_to_root",
        lambda python, root: True,
    )

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if not recreated and (
            (len(cmd) >= 3 and cmd[1:3] == ["-c", "import openai"])
            or (len(cmd) >= 4 and cmd[1:4] == ["-m", "pytest", "--version"])
        ):
            return _Proc(1, stderr="corrupt")
        return _Proc(0)

    monkeypatch.setattr("conduit.patcher.sync_env.subprocess.run", _run)

    logs: list[str] = []
    specs = sync_bumped_packages(
        {
            "rules": [
                {
                    "type": "DEPENDENCY_BUMP",
                    "package": "openai",
                    "to_version": "3.3.1",
                    "ecosystems": ["pip"],
                }
            ]
        },
        root=tmp_path,
        log=logs.append,
    )
    assert specs == ["openai==3.3.1"]
    assert recreated
    assert any("repairing verify-venv" in line.lower() for line in logs)
    assert any("--force-reinstall" in c for c in calls)
    assert any("Verify env repaired" in line for line in logs)


def test_run_tests_skips_when_pytest_unhealthy(tmp_path, monkeypatch):
    from conduit.test_runner import run_tests

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    class _Proc:
        def __init__(self, code=0, stderr=""):
            self.returncode = code
            self.stdout = ""
            self.stderr = stderr

    def _run(cmd, **kwargs):
        if len(cmd) >= 4 and cmd[1:4] == ["-m", "pytest", "--version"]:
            return _Proc(1, stderr="No module named pytest.__main__")
        return _Proc(0)

    monkeypatch.setattr("conduit.test_runner.subprocess.run", _run)
    result = run_tests(tmp_path)
    assert result.passed is True
    assert "verify_mode=skipped" in result.extra_notes
    assert "verify_kind=verify_env_unhealthy" in result.extra_notes
