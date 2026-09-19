from __future__ import annotations

from typer.testing import CliRunner

from conduit.main import app

_HELP_ENV = {"COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"}


def _help_text(*args: str) -> tuple[int, str]:
    result = CliRunner(env=_HELP_ENV).invoke(app, list(args))
    return result.exit_code, result.stdout or ""


def _panel_body(help_text: str, title: str) -> str:
    marker = f"─ {title} "
    start = help_text.find(marker)
    assert start >= 0, f"missing help panel {title!r} in:\n{help_text}"
    body_start = help_text.find("\n", start)
    assert body_start >= 0
    rest = help_text[body_start + 1 :]
    next_panel = rest.find("┌─ ")
    if next_panel >= 0:
        return rest[:next_panel]
    return rest


def _command_names(panel_body: str) -> set[str]:
    names: set[str] = set()
    for line in panel_body.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("│"):
            continue
        inner = stripped[1:].strip()
        if not inner or set(inner) <= {"─", "│", "┌", "┐", "└", "┘"}:
            continue
        name = inner.split(None, 1)[0]
        names.add(name)
    return names


def test_top_level_help_demotes_detect_and_module():
    code, text = _help_text("--help")
    assert code == 0

    commands = _command_names(_panel_body(text, "Commands"))
    advanced = _command_names(_panel_body(text, "Advanced"))

    for hero in ("packet", "watch", "apply"):
        assert hero in commands, f"{hero} missing from Commands panel:\n{text}"

    assert "detect" not in commands
    assert "module" not in commands
    assert "detect" in advanced
    assert "module" in advanced


def test_detect_help_still_works():
    code, text = _help_text("detect", "--help")
    assert code == 0
    assert "detect" in text.lower()


def test_module_list_still_works():
    code, text = _help_text("module", "list")
    assert code == 0
    assert "Detect modules" in text or "module" in text.lower()
