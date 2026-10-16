"""Terminal, Markdown, and JSON renderings of a service map."""

from __future__ import annotations

from typing import Any

from conduit.outside_in.model import Affected, BlastReport, Chain, ServiceMap


def chain_path(chain: Chain) -> str:
    parts: list[str] = []
    for hop in chain.hops:
        label = hop.symbol if hop.symbol != "<module>" else "<module>"
        parts.append(f"{hop.rel}:{hop.lineno} {label} {hop.expr}".strip())
    return " -> ".join(parts)


def service_map_to_dict(
    service_map: ServiceMap,
    blast: BlastReport | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "root": service_map.root,
        "services": service_map.services(),
        "entries": [
            {
                "service": entry.service,
                "package": entry.package,
                "kind": entry.kind,
                "rel": entry.rel,
                "lineno": entry.lineno,
                "symbol": entry.symbol,
                "confidence": entry.confidence,
                "detail": entry.detail,
            }
            for entry in service_map.entries
        ],
        "chains": [_chain_dict(chain) for chain in service_map.chains],
        "limitations": list(service_map.limitations),
        "evidence": dict(service_map.evidence),
    }
    if blast is not None:
        payload["blast"] = {
            "packet_id": blast.packet_id,
            "package": blast.package,
            "from_version": blast.from_version,
            "to_version": blast.to_version,
            "affected": [_affected_dict(item) for item in blast.affected],
        }
    else:
        payload["blast"] = None
    return payload


def format_report(
    service_map: ServiceMap,
    blast: BlastReport | None = None,
    *,
    markdown: bool = False,
) -> str:
    title = "Outside-in service map"
    lines: list[str] = []
    if markdown:
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"Root: `{service_map.root}`")
    else:
        lines.append(f"{title}")
        lines.append(f"root: {service_map.root}")
    services = service_map.services()
    lines.append(f"services: {', '.join(services) if services else '(none)'}")
    lines.append("")
    if not service_map.entries and not service_map.chains:
        lines.append("No external service entry points found.")
        lines.extend(_limitation_lines(service_map, markdown))
        return "\n".join(lines).rstrip() + "\n"

    by_service: dict[str, list] = {}
    for entry in service_map.entries:
        by_service.setdefault(entry.service, []).append(entry)
    for service in services:
        header = f"## {service}" if markdown else f"{service}"
        lines.append(header)
        shown = service_map.evidence.get(service, "")
        if markdown and shown:
            lines.append(f"evidence: `{shown}`")
        for entry in by_service.get(service, []):
            lines.append(
                f"- {entry.rel}:{entry.lineno}  {entry.kind}  {entry.symbol}  "
                f"[{entry.confidence}]"
            )
        lines.append("")

    lines.append("## Reachability" if markdown else "Reachability")
    if blast is not None:
        lines.append(
            f"packet: {blast.packet_id}  {blast.package} "
            f"{blast.from_version} -> {blast.to_version}"
        )
    chains = [item.chain for item in blast.affected] if blast is not None else list(service_map.chains)
    break_by_site = {}
    if blast is not None:
        for item in blast.affected:
            break_by_site[_site_key(item.chain)] = item
    if not chains:
        lines.append("(no call chains)")
    for chain in chains:
        lines.append("")
        wrapper = "wrapper" if chain.via_wrapper else "direct"
        lines.append(
            f"- {chain.call_site}  {chain.call_symbol}  {chain.call_expr}  "
            f"({wrapper}, {chain.confidence})"
        )
        lines.append(f"  path: {chain_path(chain)}")
        if chain.params and chain.kind != "webhook":
            lines.append(f"  sdk params: {', '.join(chain.params)}")
        if chain.response_fields:
            label = "payload fields" if chain.kind == "webhook" else "response fields"
            lines.append(f"  {label}: {', '.join(chain.response_fields)}")
        item = break_by_site.get(_site_key(chain))
        if item is not None:
            if item.breaks:
                lines.append("  breaks:")
                for issue in item.breaks:
                    lines.append(f"    - {issue.detail} [{issue.confidence}]")
            else:
                lines.append("  breaks: (packet does not name a change for this call)")
    lines.extend(_limitation_lines(service_map, markdown))
    return "\n".join(lines).rstrip() + "\n"


def _site_key(chain: Chain) -> tuple[str, int, str, str]:
    return (chain.call_rel, chain.call_lineno, chain.call_symbol, chain.sdk_method)


def _limitation_lines(service_map: ServiceMap, markdown: bool) -> list[str]:
    if not service_map.limitations:
        return []
    lines = ["", "## Limitations" if markdown else "Limitations"]
    for item in service_map.limitations:
        lines.append(f"- {item}")
    return lines


def _chain_dict(chain: Chain) -> dict[str, Any]:
    return {
        "service": chain.service,
        "package": chain.package,
        "call_site": chain.call_site,
        "rel": chain.call_rel,
        "lineno": chain.call_lineno,
        "symbol": chain.call_symbol,
        "expr": chain.call_expr,
        "sdk_method": chain.sdk_method,
        "kind": chain.kind,
        "via_wrapper": chain.via_wrapper,
        "params": list(chain.params),
        "site_keywords": list(chain.site_keywords),
        "response_fields": list(chain.response_fields),
        "confidence": chain.confidence,
        "hops": [
            {
                "rel": hop.rel,
                "lineno": hop.lineno,
                "symbol": hop.symbol,
                "expr": hop.expr,
            }
            for hop in chain.hops
        ],
        "path": chain_path(chain),
    }


def _affected_dict(item: Affected) -> dict[str, Any]:
    payload = _chain_dict(item.chain)
    payload["breaks"] = [
        {
            "kind": issue.kind,
            "rule_type": issue.rule_type,
            "old": issue.old,
            "new": issue.new,
            "detail": issue.detail,
            "confidence": issue.confidence,
        }
        for issue in item.breaks
    ]
    return payload
