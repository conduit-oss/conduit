"""Copy-paste human checklist for mergeable PRs (side_effects + leftovers)."""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def format_side_effect_row(effect: dict[str, Any]) -> str:
    """One line for a packet side_effect; prefer structured gap fields."""
    gap = str(effect.get("gap_kind") or "").strip()
    old = str(effect.get("old_shape") or "").strip()
    blocker = str(effect.get("blocker") or "").strip()
    detail = str(effect.get("detail") or "").strip()
    kind = str(effect.get("kind") or "other").strip() or "other"
    if gap or old or blocker:
        parts: list[str] = []
        if gap:
            parts.append(gap)
        if old:
            parts.append(old)
        if blocker:
            parts.append(blocker)
        elif detail:
            parts.append(detail)
        return f"[{kind}] " + " — ".join(parts)
    if detail:
        return f"Side effect ({kind}): {detail}"
    return f"Side effect ({kind})"


def side_effect_checklist_lines(packet: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for effect in packet.get("side_effects") or []:
        if not isinstance(effect, dict):
            continue
        line = format_side_effect_row(effect)
        if line.strip():
            rows.append(line)
    return rows


def leftover_checklist_lines(leftovers: Iterable[Any]) -> list[str]:
    rows: list[str] = []
    for item in leftovers:
        display = getattr(item, "display", None)
        if callable(display):
            text = str(display()).strip()
        elif isinstance(item, str):
            text = item.strip()
        else:
            text = str(item).strip()
        if text:
            rows.append(text)
    return rows


def build_human_checklist(
    packet: dict[str, Any],
    *,
    leftovers: Sequence[Any] | None = None,
) -> list[str]:
    """Return checklist bullet texts (no heading). Empty when nothing to review."""
    lines: list[str] = []
    for row in side_effect_checklist_lines(packet):
        lines.append(row)
    for row in leftover_checklist_lines(leftovers or ()):
        lines.append(f"leftover: {row}")
    return lines


def format_human_checklist(
    packet: dict[str, Any],
    *,
    leftovers: Sequence[Any] | None = None,
    heading: str = "Human checks (copy into PR)",
) -> str:
    """Plain-text block for CLI / evidence. Empty string when nothing to review."""
    bullets = build_human_checklist(packet, leftovers=leftovers)
    if not bullets:
        return ""
    out = [f"=== {heading} ===", "Mechanical merge bar: leftover-clean only."]
    out.append("Anything below needs human review or redesign assist:")
    for b in bullets:
        out.append(f"- {b}")
    return "\n".join(out)


def format_human_checklist_markdown(
    packet: dict[str, Any],
    *,
    leftovers: Sequence[Any] | None = None,
) -> str:
    bullets = build_human_checklist(packet, leftovers=leftovers)
    if not bullets:
        return ""
    lines = [
        "### Double-check (human)",
        "",
        "Mechanical merge bar is leftover-clean. Review or redesign:",
        "",
    ]
    lines.extend(f"- {b}" for b in bullets)
    return "\n".join(lines)
