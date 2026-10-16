"""Blast radius: which outside-in chains a Migration Packet would break."""

from __future__ import annotations

from urllib.parse import urlparse

from conduit.outside_in.graph import min_confidence
from conduit.outside_in.model import Affected, BlastReport, Break, Chain, ServiceMap


def blast_radius(service_map: ServiceMap, packet: dict) -> BlastReport:
    package = str(packet.get("package") or "").strip()
    report = BlastReport(
        packet_id=str(packet.get("packet_id") or ""),
        package=package,
        from_version=str(packet.get("from_version") or ""),
        to_version=str(packet.get("to_version") or ""),
        affected=(),
    )
    if not package:
        return report
    rules = [rule for rule in (packet.get("rules") or []) if isinstance(rule, dict)]
    affected: list[Affected] = []
    for chain in service_map.chains:
        if chain.package != package and chain.service != package:
            continue
        # Subscript reads and exception references are impact sites, not packet calls.
        if chain.kind.endswith("_response") or chain.kind.endswith("_ref"):
            continue
        breaks = tuple(_breaks_for(chain, rules))
        affected.append(Affected(chain=chain, breaks=breaks))
    report = BlastReport(
        packet_id=report.packet_id,
        package=report.package,
        from_version=report.from_version,
        to_version=report.to_version,
        affected=tuple(affected),
    )
    return report


def _breaks_for(chain: Chain, rules: list[dict]) -> list[Break]:
    successors = {chain.sdk_method}
    found: list[Break] = []
    for rule in rules:
        if str(rule.get("type") or "") != "AST_CALL_REWRITE":
            continue
        old = str(rule.get("old_callee") or "").strip()
        new = str(rule.get("new_callee") or "").strip()
        matched, confidence = _matches_callee(chain, old)
        if not matched:
            continue
        if new:
            successors.add(new)
        if chain.kind == "http_call":
            detail = f"REST call matches packet callee {old} -> {new}"
        else:
            detail = f"method renamed: {old} -> {new}"
        found.append(
            Break(
                kind="method_renamed",
                rule_type="AST_CALL_REWRITE",
                old=old,
                new=new,
                detail=detail,
                confidence=confidence,
            )
        )
    for rule in rules:
        rtype = str(rule.get("type") or "")
        if rtype == "AST_PARAM_RENAME":
            target = str(rule.get("function_target") or "").strip()
            old = str(rule.get("old_param") or "").strip()
            new = str(rule.get("new_param") or "").strip()
            if not old or not _target_matches_any(target, successors):
                continue
            if not _param_visible(chain, old):
                continue
            found.append(
                Break(
                    kind="param_renamed",
                    rule_type=rtype,
                    old=old,
                    new=new,
                    detail=f"param renamed: {old} -> {new} on {target}",
                    confidence=chain.confidence,
                )
            )
        elif rtype == "AST_PARAM_DROP":
            target = str(rule.get("function_target") or "").strip()
            old = str(rule.get("param") or "").strip()
            if not old or not _target_matches_any(target, successors):
                continue
            if not _param_visible(chain, old):
                continue
            found.append(
                Break(
                    kind="param_removed",
                    rule_type=rtype,
                    old=old,
                    new="",
                    detail=f"param removed: {old} on {target}",
                    confidence=chain.confidence,
                )
            )
        elif rtype == "AST_ATTR_RENAME":
            old = str(rule.get("old_attr") or "").strip()
            new = str(rule.get("new_attr") or "").strip()
            if old and _field_mentions(chain.response_fields, old):
                found.append(
                    Break(
                        kind="response_attr_renamed",
                        rule_type=rtype,
                        old=old,
                        new=new,
                        detail=f"response attribute renamed: {old} -> {new}",
                        confidence=chain.confidence,
                    )
                )
        elif rtype == "KEY_RENAME":
            old = str(rule.get("old_key") or "").strip()
            new = str(rule.get("new_key") or "").strip()
            if old and _field_mentions(chain.response_fields, old):
                found.append(
                    Break(
                        kind="response_key_renamed",
                        rule_type=rtype,
                        old=old,
                        new=new,
                        detail=f"response key renamed: {old} -> {new}",
                        confidence=chain.confidence,
                    )
                )
    return _dedupe_breaks(found)


def _param_visible(chain: Chain, name: str) -> bool:
    """True when ``name`` is passed somewhere on this chain.

    Direct calls pass it on the SDK call (``params``). A wrapper chain
    inherits that same break: if an inner hop sets the keyword, every outer
    call site on the chain is affected. A keyword only on this call site
    still counts. Confidence stays ``chain.confidence``.
    """
    if name in chain.params:
        return True
    return chain.via_wrapper and name in chain.site_keywords


def _target_matches_any(target: str, methods: set[str]) -> bool:
    if not target:
        return False
    return any(_callee_equals(method, target) for method in methods if method)


def _matches_callee(chain: Chain, old: str) -> tuple[bool, str]:
    if not old:
        return False, chain.confidence
    if chain.kind == "http_call":
        path = _http_path(chain.sdk_method)
        mapped = _path_for_callee(old)
        if path and mapped and (path == mapped or path.startswith(mapped + "/")):
            return True, min_confidence(chain.confidence, "medium")
        return False, chain.confidence
    if chain.kind == "sdk_constructor":
        return False, chain.confidence
    if _callee_equals(chain.sdk_method, old):
        return True, chain.confidence
    return False, chain.confidence


def _callee_equals(method: str, old: str) -> bool:
    if method == old:
        return True
    return method.endswith("." + old)


def _http_path(sdk_method: str) -> str | None:
    if "://" not in sdk_method:
        return None
    url = sdk_method.split(" ", 1)[-1]
    path = urlparse(url).path
    return path or None


def _path_for_callee(callee: str) -> str | None:
    """REST path embedded in a callee, when the callee is itself a path."""
    text = (callee or "").strip()
    if text.startswith("/") or "://" in text:
        path = urlparse(text).path if "://" in text else text
        return path or None
    return None


def _field_mentions(fields: tuple[str, ...], token: str) -> bool:
    needle_attr = "." + token
    needle_sq = "['" + token + "']"
    needle_dq = '["' + token + '"]'
    for field in fields:
        if needle_attr in field or field.startswith(token):
            return True
        if needle_sq in field or needle_dq in field:
            return True
    return False


_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _dedupe_breaks(items: list[Break]) -> list[Break]:
    """One line per distinct change at this call site.

    Packets often spell one callee twice (``package.Resource.create`` and
    ``Resource.create``, or the same pair matched to one REST path).
    Same kind and same destination are one rename; keep the more specific
    callee. A different destination stays its own line.
    """
    out: list[Break] = []
    for item in items:
        for index, prev in enumerate(out):
            if _same_change(prev, item):
                out[index] = _prefer_break(prev, item)
                break
        else:
            out.append(item)
    return out


def _same_change(left: Break, right: Break) -> bool:
    if left.kind != right.kind or left.new != right.new:
        return False
    if left.old == right.old:
        return True
    return _callee_equals(left.old, right.old) or _callee_equals(right.old, left.old)


def _prefer_break(current: Break, incoming: Break) -> Break:
    if _more_specific_callee(incoming.old, current.old):
        return incoming
    if _more_specific_callee(current.old, incoming.old):
        return current
    if _CONF_RANK.get(incoming.confidence, 1) > _CONF_RANK.get(current.confidence, 1):
        return incoming
    return current


def _more_specific_callee(left: str, right: str) -> bool:
    return bool(right) and left != right and _callee_equals(left, right)
