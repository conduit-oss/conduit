"""Turn feed hits into a schema-valid migration packet."""

from __future__ import annotations

import re
from collections import defaultdict

from conduit.feeds.model import FeedHit
from conduit.feeds.versions import parse_version
from conduit.packet.validate import validate_packet

_SOURCE_KIND = {
    "github_releases": "github_release",
    "pypi": "other",
    "npm": "other",
    "openapi": "openapi",
    "changelog": "changelog",
}


def packets_from_hits(hits: list[FeedHit]) -> tuple[list[dict], list[str]]:
    """Group hits for one package hop and build packets.

    Returns ``(packets, problems)``. A packet that fails the schema is
    dropped and described in ``problems``.
    """
    groups: dict[tuple[str, str, str, str], list[FeedHit]] = defaultdict(list)
    loose: list[FeedHit] = []
    for hit in hits:
        if hit.from_version or hit.to_version:
            key = (hit.package, hit.ecosystem, hit.from_version, hit.to_version)
            groups[key].append(hit)
        else:
            loose.append(hit)
    for hit in loose:
        candidates = [key for key in groups if key[0] == hit.package and key[1] == hit.ecosystem]
        if not candidates:
            groups[(hit.package, hit.ecosystem, "", "")].append(hit)
            continue
        best = max(candidates, key=lambda key: _version_key(key[3]))
        groups[best].append(hit)

    packets: list[dict] = []
    problems: list[str] = []
    for key in sorted(groups):
        packet = _build(key, groups[key])
        if packet is None:
            continue
        errors = validate_packet(packet)
        if errors:
            problems.append(f"{packet.get('packet_id')}: {'; '.join(errors)}")
            continue
        packets.append(packet)
    return packets, problems


def _build(key: tuple[str, str, str, str], hits: list[FeedHit]) -> dict | None:
    package, ecosystem, from_version, to_version = key
    rules = _rules(package, ecosystem, from_version, to_version, hits)
    effects = _side_effects(hits)
    if not rules and not effects:
        return None
    sources = _sources(hits)
    notes = _notes(package, ecosystem, from_version, to_version, hits)
    return {
        "packet_id": _packet_id(package, ecosystem, from_version, to_version),
        "package": package,
        "ecosystem": ecosystem if ecosystem in {"pypi", "npm", "go", "maven", "other"} else "other",
        "from_version": from_version,
        "to_version": to_version,
        "sources": sources,
        "notes": notes,
        "side_effects": effects,
        "rules": rules,
    }


def _rules(
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    hits: list[FeedHit],
) -> list[dict]:
    rules: list[dict] = []
    version_hits = [hit for hit in hits if hit.change == "version" and hit.from_version and hit.to_version]
    if version_hits:
        best = max(version_hits, key=lambda hit: hit.rank())
        rule: dict = {
            "type": "DEPENDENCY_BUMP",
            "package": package,
            "from_version": from_version,
            "to_version": to_version,
            "reason": f"{best.source} ({best.confidence}): {best.detail}",
        }
        ecosystems = _ecosystems(ecosystem)
        if ecosystems:
            rule["ecosystems"] = ecosystems
        rules.append(rule)

    callee = ""
    best_rank = -1
    for hit in hits:
        if hit.change == "call_rename" and hit.new and hit.rank() >= best_rank:
            callee = hit.new
            best_rank = hit.rank()

    seen_calls: set[tuple[str, str]] = set()
    for hit in _best_by(hits, "call_rename", lambda hit: (hit.old, hit.new)):
        for old, new in _call_forms(hit.old, hit.new):
            if (old, new) in seen_calls:
                continue
            seen_calls.add((old, new))
            reason = f"{hit.source} ({hit.confidence}): {old} renamed to {new}"
            if old != hit.old:
                reason = f"{hit.source} ({hit.confidence}): unqualified form of {hit.old} -> {new}"
            rules.append(
                {
                    "type": "AST_CALL_REWRITE",
                    "target_files": ["*.py"],
                    "old_callee": old,
                    "new_callee": new,
                    "reason": reason,
                }
            )

    seen_params: set[tuple[str, str, str]] = set()
    for hit in _best_by(hits, "param_rename", lambda hit: (hit.old, hit.new, hit.target)):
        if hit.source == "openapi" or " " in (hit.target or ""):
            continue
        target = hit.target or callee
        if not target or not hit.old or not hit.new:
            continue
        identity = (target, hit.old, hit.new)
        if identity in seen_params:
            continue
        seen_params.add(identity)
        rules.append(
            {
                "type": "AST_PARAM_RENAME",
                "target_files": ["*.py"],
                "function_target": target,
                "old_param": hit.old,
                "new_param": hit.new,
                "reason": f"{hit.source} ({hit.confidence}): parameter {hit.old} renamed to {hit.new}",
            }
        )
    return rules


def _side_effects(hits: list[FeedHit]) -> list[dict]:
    effects: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        if hit.change == "path_rename":
            blocker = "public spec diff does not name the SDK callee"
        elif hit.change == "removed":
            blocker = "public spec removed this operation and did not name a successor"
        elif hit.change == "param_rename" and (hit.source == "openapi" or " " in (hit.target or "")):
            blocker = "public spec renamed a parameter; no SDK function name in the diff"
        else:
            continue
        identity = (hit.change, hit.old, hit.new)
        if identity in seen:
            continue
        seen.add(identity)
        effect: dict = {
            "kind": "other",
            "detail": hit.detail or f"{hit.old} -> {hit.new}",
            "gap_kind": "external",
            "old_shape": hit.old or hit.detail,
            "blocker": blocker,
        }
        if hit.new:
            effect["new_shape"] = hit.new
        if hit.url:
            effect["evidence_url"] = hit.url
        effects.append(effect)
    return effects


def _sources(hits: list[FeedHit]) -> list[dict]:
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        kind = _SOURCE_KIND.get(hit.source, "other")
        url = hit.url or f"conduit://feed/{hit.source}"
        identity = (url, kind)
        if identity in seen:
            continue
        seen.add(identity)
        found.append({"url": url, "kind": kind})
    return found


def _notes(
    package: str,
    ecosystem: str,
    from_version: str,
    to_version: str,
    hits: list[FeedHit],
) -> str:
    hop = f"{from_version or '?'} -> {to_version or '?'}"
    lines = [
        f"Feed watch for {package} ({ecosystem}) {hop}.",
        "Changelog and deprecation pages are the lowest-confidence source.",
    ]
    for hit in sorted(hits, key=lambda item: (item.source, item.change, item.old, item.new)):
        lines.append(f"- {hit.source} ({hit.confidence}): {hit.detail}")
    return "\n".join(lines)


def _best_by(hits: list[FeedHit], change: str, key):
    chosen: dict[tuple, FeedHit] = {}
    for hit in hits:
        if hit.change != change:
            continue
        identity = key(hit)
        current = chosen.get(identity)
        if current is None or hit.rank() > current.rank():
            chosen[identity] = hit
    return list(chosen.values())


def _call_forms(old: str, new: str) -> list[tuple[str, str]]:
    forms = [(old, new)]
    parts = [part for part in old.split(".") if part]
    if len(parts) >= 3 and parts[-2][:1].isupper():
        tail = ".".join(parts[-2:])
        if tail != old:
            forms.append((tail, new))
    return forms


def _ecosystems(ecosystem: str) -> list[str]:
    if ecosystem == "pypi":
        return ["pip", "pyproject"]
    if ecosystem == "npm":
        return ["npm"]
    return []


def _packet_id(package: str, ecosystem: str, from_version: str, to_version: str) -> str:
    raw = f"{package}-{ecosystem}-{from_version or 'none'}-{to_version or 'unversioned'}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", raw)


def _version_key(value: str) -> tuple:
    parsed = parse_version(value)
    if parsed is None:
        return (0, value)
    return (1, parsed)
