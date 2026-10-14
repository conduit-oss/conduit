"""Text and JSON for line-by-line impact."""

from __future__ import annotations

from conduit.impact.model import Impact


def format_impacts(impacts: list[Impact]) -> str:
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
            for site in item.sites:
                expr = f"  {site.expr}" if site.expr else ""
                lines.append(f"    {site.path}:{site.line}{expr} [{site.confidence}]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def impacts_to_dicts(impacts: list[Impact]) -> list[dict]:
    return [item.to_dict() for item in impacts]
