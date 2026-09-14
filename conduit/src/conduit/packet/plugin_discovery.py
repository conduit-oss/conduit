"""Discover packet plugins via entry points and resolve by name/package."""

from __future__ import annotations

import os
from typing import Iterable

from conduit.packet.plugin import PacketPlugin

ENTRY_POINT_GROUP = "conduit.packet_plugins"
ENV_PLUGIN = "CONDUIT_PACKET_PLUGIN"
PLUGIN_NONE = "none"


class PluginResolveError(ValueError):
    """Ambiguous or missing packet plugin."""


def _entry_point_plugins() -> list[PacketPlugin]:
    from importlib.metadata import entry_points

    plugins: list[PacketPlugin] = []
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        eps = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[assignment]
    for ep in eps:
        try:
            obj = ep.load()
            plug = obj() if isinstance(obj, type) else obj
            if isinstance(plug, PacketPlugin) or (
                hasattr(plug, "name")
                and hasattr(plug, "matches")
                and hasattr(plug, "propose")
                and hasattr(plug, "guide_enrich")
                and hasattr(plug, "after_enrich")
            ):
                plugins.append(plug)  # type: ignore[arg-type]
        except Exception:
            continue
    return plugins


def load_plugins(*, names: Iterable[str] | None = None) -> list[PacketPlugin]:
    """Return registered packet plugins, optionally filtered by name."""
    seen: set[str] = set()
    out: list[PacketPlugin] = []
    for plug in _entry_point_plugins():
        name = str(getattr(plug, "name", "") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(plug)
    if names is not None:
        wanted = {n.lower() for n in names}
        out = [p for p in out if str(p.name).lower() in wanted]
    return out


def resolve_plugin(
    *,
    package: str,
    ecosystem: str = "pypi",
    plugin: str | None = None,
    plugins: list[PacketPlugin] | None = None,
) -> PacketPlugin | None:
    """
    Resolve a packet plugin.

    ``plugin``:
      - ``None``: use ``CONDUIT_PACKET_PLUGIN`` if set, else auto-match by package
      - ``\"none\"``: force no plugin
      - name: require that plugin
    """
    eco = (ecosystem or "pypi").strip().lower() or "pypi"
    pkg = (package or "").strip()
    raw = (plugin if plugin is not None else os.environ.get(ENV_PLUGIN) or "").strip()
    if raw.lower() == PLUGIN_NONE:
        return None

    available = list(plugins) if plugins is not None else load_plugins()

    if raw:
        want = raw.lower()
        for plug in available:
            if str(plug.name).lower() == want:
                return plug
        known = ", ".join(sorted(str(p.name) for p in available)) or "(none)"
        raise PluginResolveError(
            f"Packet plugin {raw!r} not found. Known: {known}"
        )

    matches = [p for p in available if p.matches(pkg, eco)]
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(sorted(str(p.name) for p in matches))
        raise PluginResolveError(
            f"Multiple packet plugins match package {pkg!r}: {names}. "
            f"Pass --plugin <name> or --plugin none."
        )
    return matches[0]
