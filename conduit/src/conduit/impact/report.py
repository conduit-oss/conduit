"""Text and JSON for line-by-line impact."""

from __future__ import annotations

from conduit.impact.model import Impact


def format_impacts(impacts: list[Impact], *, show_callers: bool = False) -> str:
    if not impacts:
        return (
            "No call sites in this repo are hit by the released change.\n"
            "Ordinary library bumps are not reported.\n"
        )
    lines: list[str] = []
    groups: list[tuple[str, str, str, list[Impact]]] = []
    index: dict[tuple[str, str, str], list[Impact]] = {}
    for item in impacts:
        key = (item.dependency, item.from_version, item.to_version)
        if key not in index:
            bucket: list[Impact] = []
            index[key] = bucket
            groups.append((key[0], key[1], key[2], bucket))
        index[key].append(item)
    for dependency, from_version, to_version, items in groups:
        lines.append(f"{dependency} {from_version} -> {to_version}")
        for item in items:
            lines.append(f"  {item.change} [{item.confidence}]")
            lines.extend(_format_sites(item, show_callers=show_callers))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_sites(item: Impact, *, show_callers: bool) -> list[str]:
    primary = [site for site in item.sites if not site.via_wrapper]
    callers = [site for site in item.sites if site.via_wrapper]
    lines: list[str] = []
    for site in primary or callers:
        expr = f"  {site.expr}" if site.expr else ""
        lines.append(f"    {site.path}:{site.line}{expr} [{site.confidence}]")
    if primary and callers:
        lines.append("    low confidence (excluded from the breaking-line count):")
        if show_callers:
            noun = "wrapper caller" if len(callers) == 1 else "wrapper callers"
            lines.append(f"      {noun} ({len(callers)}):")
            for site in callers:
                expr = f"  {site.expr}" if site.expr else ""
                lines.append(f"        {site.path}:{site.line}{expr} [{site.confidence}]")
        else:
            noun = "wrapper caller" if len(callers) == 1 else "wrapper callers"
            lines.append(f"      {len(callers)} {noun}; pass --show-callers to list")
    return lines


def impacts_to_dicts(impacts: list[Impact]) -> list[dict]:
    return [item.to_dict() for item in impacts]
