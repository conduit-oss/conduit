"""Tests for post-apply pip sync of DEPENDENCY_BUMP pins."""

from __future__ import annotations

from conduit.patcher.sync_env import bumped_package_specs, sync_bumped_packages


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


def test_detect_test_command_uses_sys_executable(tmp_path):
    import sys

    from conduit.test_runner import detect_test_command

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
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
    # Smoke checks after successful install.
    assert any(c[1:3] == ["-c", "import openai"] for c in calls)
    assert any(c[1:4] == ["-m", "pytest", "--version"] for c in calls)


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
        # Fail smoke until verify-venv has been recreated.
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
