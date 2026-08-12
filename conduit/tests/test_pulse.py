"""Conduit pulse: flowing frames + rotating one-word families."""

from __future__ import annotations

from rich.console import Console

from conduit.pulse import (
    FAMILIES,
    FRAMES,
    _BODY_WIDTH,
    beat,
    family_for_tool,
    flowing,
    pulse_enabled,
    render_conduit_frame,
    start_pulse,
    stop_pulse,
    words_for,
)


def test_frames_are_a_wrapping_loop():
    assert len(FRAMES) == _BODY_WIDTH
    blob = "".join(FRAMES)
    assert "*" not in blob and "°" not in blob
    for i, body in enumerate(FRAMES):
        assert len(body) == _BODY_WIDTH
        assert "█" in body
        nxt = FRAMES[(i + 1) % len(FRAMES)]
        # Head advances one cell and wraps.
        assert nxt == body[-1] + body[:-1]


def test_render_conduit_frame_is_compact_pipe():
    text = render_conduit_frame(3, "pondering")
    plain = text.plain
    assert "\n" not in plain
    assert plain.startswith("╞")
    assert "pondering" in plain
    assert "idle" not in plain
    assert "motion" not in plain
    assert "pressure" not in plain


def test_pressure_glyphs_use_lighter_blue():
    from conduit.pulse import _CHAR_STYLE

    assert _CHAR_STYLE["░"] == "blue"
    assert _CHAR_STYLE["≈"] == "bright_blue"
    assert _CHAR_STYLE["▓"] == "cyan"
    assert "bright_cyan" in _CHAR_STYLE["█"]


def test_words_are_single_tokens():
    for family, words in FAMILIES.items():
        assert words
        for word in words:
            assert " " not in word
            assert word.islower()
        assert family.islower()


def test_family_for_tool_maps_agent_actions():
    assert family_for_tool("grep") == "grep"
    assert family_for_tool("read_file") == "read"
    assert family_for_tool("write_file") == "write"
    assert family_for_tool("web_search") == "search"
    assert family_for_tool("fetch_url") == "fetch"
    assert family_for_tool("run_tests") == "test"
    assert family_for_tool("run_shell") == "shell"
    assert family_for_tool("mystery_tool") == "think"


def test_words_for_accepts_family_or_verb():
    assert "pondering" in words_for("think")
    assert "sifting" in words_for("grep")
    assert words_for("pondering") == FAMILIES["think"]
    assert words_for("moonwalking") == ("moonwalking",)


def test_pulse_disabled_in_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_pulse.py::test")
    console = Console(force_terminal=True)
    assert pulse_enabled(console) is False
    start_pulse(console, "think")
    beat("grep")
    stop_pulse()


def test_flowing_context_no_raise_when_disabled(monkeypatch):
    monkeypatch.setenv("CONDUIT_NO_PULSE", "1")
    console = Console(force_terminal=True)
    with flowing(console, "think"):
        beat("write")
