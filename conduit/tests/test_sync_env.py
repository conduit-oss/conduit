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
    assert logs and "Installing bumped packages" in logs[0]
