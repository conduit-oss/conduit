"""Animated conduit flow while work is in flight."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.live import Live
from rich.text import Text

# Visible 16-color blues (dark hex vanished on dark terminals).
# ░ dark  →  ≈ mid  →  ▓ bright  →  █ lightest (pressure).
_CHAR_STYLE = {
    "╞": "blue",
    "╡": "blue",
    "░": "blue",
    "≈": "bright_blue",
    "▓": "cyan",
    "█": "bold bright_cyan",
}

# Pressure head █ travels right, wraps, and re-enters — a closed loop.
_SEED = "█▓≈≈░░░░"
FRAMES = tuple(_SEED[-i:] + _SEED[:-i] if i else _SEED for i in range(len(_SEED)))

# Family name -> one-word verbs that rotate while that kind of work runs.
FAMILIES: dict[str, tuple[str, ...]] = {
    "awakening": ("awakening", "stirring", "uncoiling"),
    "detect": ("sniffing", "scrying", "divining", "eavesdropping"),
    "prune": ("shearing", "culling", "trimming", "winnowing"),
    "export": ("charting", "mapping", "surveying"),
    "packet": ("weaving", "forging", "bundling", "braiding"),
    "apply": ("splicing", "grafting", "rewiring", "stitching"),
    "hatch": ("hatching", "spinning", "sprouting"),
    "test": ("proving", "gauging", "stressing", "prodding"),
    "repair": ("mending", "soldering", "darning", "patching"),
    "pr": ("launching", "offering", "unfurling"),
    "think": ("pondering", "mulling", "scheming", "brewing", "simmering", "daydreaming"),
    "search": ("foraging", "trawling", "hunting", "prospecting"),
    "fetch": ("reeling", "hauling", "siphoning"),
    "read": ("peeking", "skimming", "tracing"),
    "grep": ("sifting", "rummaging", "panning"),
    "write": ("scribing", "etching", "inscribing"),
    "list": ("cataloging", "inventorying"),
    "shell": ("tinkering", "fiddling", "wrenching"),
    "wait": ("cooling", "idling", "drifting"),
}

_TOOL_FAMILY = {
    "web_search": "search",
    "file_search": "search",
    "fetch_url": "fetch",
    "read_file": "read",
    "list_files": "list",
    "grep": "grep",
    "write_file": "write",
    "run_tests": "test",
    "run_shell": "shell",
    "code_interpreter": "shell",
    "mcp": "fetch",
}

_FRAME_SEC = 0.12
_WORD_SWAP_SEC = 1.2
_BODY_WIDTH = 8

_lock = threading.Lock()
_current: "_Pulse | None" = None


def family_for_tool(name: str) -> str:
    raw = (name or "").strip().lower()
    if raw in _TOOL_FAMILY:
        return _TOOL_FAMILY[raw]
    if raw in FAMILIES:
        return raw
    return "think"


def paint_line(raw: str) -> Text:
    text = Text()
    for ch in raw:
        text.append(ch, style=_CHAR_STYLE.get(ch, "cyan"))
    return text


def render_conduit_frame(index: int, word: str = "") -> Text:
    """Build one compact pipe frame, optionally with a status word."""
    out = paint_line(f"╞{FRAMES[index % len(FRAMES)]}╡")
    if word:
        out.append("  ")
        out.append(word, style="bold bright_blue")
    return out


def words_for(family: str) -> tuple[str, ...]:
    key = (family or "think").strip().lower()
    if key in FAMILIES:
        return FAMILIES[key]
    for name, words in FAMILIES.items():
        if key in words:
            return words
        if key == name:
            return words
    return (key or "flowing",)


def pulse_enabled(console: Console | None = None) -> bool:
    flag = os.environ.get("CONDUIT_NO_PULSE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if console is not None and not console.is_terminal:
        return False
    return True


class _Pulse:
    def __init__(self, console: Console, family: str) -> None:
        self.console = console
        self._family = family
        self._word_idx = 0
        self._frame = 0
        self._stop = threading.Event()
        self._state = threading.Lock()
        self._live: Live | None = None
        self._thread: threading.Thread | None = None

    def _render(self) -> Text:
        words = words_for(self._family)
        word = words[self._word_idx % len(words)]
        return render_conduit_frame(self._frame, word)

    def start(self) -> None:
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=8,
            transient=True,
            vertical_overflow="crop",
        )
        self._live.start()
        self._thread = threading.Thread(target=self._loop, name="conduit-pulse", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_word = time.monotonic()
        while not self._stop.wait(_FRAME_SEC):
            now = time.monotonic()
            with self._state:
                self._frame += 1
                if now - last_word >= _WORD_SWAP_SEC:
                    self._word_idx += 1
                    last_word = now
                render = self._render()
            live = self._live
            if live is not None:
                try:
                    live.update(render)
                except Exception:
                    break

    def set_family(self, family: str) -> None:
        key = (family or "think").strip().lower()
        with self._state:
            if key != self._family:
                self._family = key
                self._word_idx = 0

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.6)
        live = self._live
        self._live = None
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass


def start_pulse(console: Console, family: str = "awakening") -> None:
    global _current
    with _lock:
        if _current is not None:
            _current.set_family(family)
            return
        if not pulse_enabled(console):
            return
        pulse = _Pulse(console, family)
        try:
            pulse.start()
        except Exception:
            return
        _current = pulse


def stop_pulse() -> None:
    global _current
    with _lock:
        pulse = _current
        _current = None
    if pulse is not None:
        pulse.stop()


def beat(family: str) -> None:
    """Switch the rotating status-word family if a pulse is running."""
    with _lock:
        pulse = _current
    if pulse is not None:
        pulse.set_family(family)


@contextmanager
def flowing(console: Console, family: str = "awakening") -> Iterator[None]:
    start_pulse(console, family)
    try:
        yield
    finally:
        stop_pulse()
