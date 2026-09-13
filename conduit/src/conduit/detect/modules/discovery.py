"""Discover built-in and entry-point detect modules."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Iterable

from conduit.detect.modules.base import DetectModule


def _builtin_modules() -> list[DetectModule]:
    from conduit.detect.modules.google_genai import GoogleGenaiModule
    from conduit.detect.modules.openai import OpenAIModule

    return [OpenAIModule(), GoogleGenaiModule()]


def _entry_point_modules() -> list[DetectModule]:
    modules: list[DetectModule] = []
    try:
        eps = entry_points(group="conduit.detect_modules")
    except TypeError:
        # Python < 3.10 style (shouldn't hit with requires-python >=3.10)
        eps = entry_points().get("conduit.detect_modules", [])  # type: ignore[assignment]
    for ep in eps:
        try:
            obj = ep.load()
            mod = obj() if isinstance(obj, type) else obj
            if isinstance(mod, DetectModule):
                modules.append(mod)
        except Exception:
            continue
    return modules


def load_modules(*, names: Iterable[str] | None = None) -> list[DetectModule]:
    """Return detect modules, optionally filtered by name."""
    seen: set[str] = set()
    out: list[DetectModule] = []
    for mod in _builtin_modules() + _entry_point_modules():
        if mod.name in seen:
            continue
        seen.add(mod.name)
        out.append(mod)
    if names is not None:
        wanted = {n.lower() for n in names}
        out = [m for m in out if m.name.lower() in wanted]
    return out
