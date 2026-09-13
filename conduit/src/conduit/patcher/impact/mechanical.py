"""Mechanical impact inference from planned touches and vendor markers."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from conduit.anticheat.rules import old_kwargs_on_new_callee_findings
from conduit.patcher.impact.templates import azure_bridge_module_body
from conduit.patcher.impact.vendor import default_banned_kwargs_on, impact_classes
from conduit.test_gen import oracle_forbidden_tokens, token_in_text

_STILL_USES_RENAME_RE = re.compile(
    r"still uses (?P<old>\w+)=\s*\(migrate to (?P<new>\w+)=\)",
    re.I,
)


def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def _glob_ok(rel: str, patterns: list[str]) -> bool:
    name = Path(rel).name
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


def _has_marker(text: str, markers: list[str]) -> bool:
    return any(m and m in text for m in markers)


def packet_covers_kwarg_finding(packet: dict[str, Any], detail: str) -> bool:
    """True when finding is already fixed by an AST_PARAM_RENAME in the packet."""
    m = _STILL_USES_RENAME_RE.search(detail or "")
    if not m:
        return False
    old = m.group("old")
    new = m.group("new")
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "AST_PARAM_RENAME":
            continue
        if str(rule.get("old_param") or "") != old:
            continue
        if str(rule.get("new_param") or "") != new:
            continue
        return True
    return False


def _packet_with_banned_kwargs(
    packet: dict[str, Any], vendor_kwargs: dict[str, list[str]]
) -> dict[str, Any]:
    if not vendor_kwargs:
        return packet
    merged = dict(packet)
    anticheat = dict(packet.get("anticheat") or {})
    existing = dict(anticheat.get("banned_kwargs_on") or {})
    for callee, kwargs in vendor_kwargs.items():
        if callee not in existing:
            existing[callee] = list(kwargs)
    anticheat["banned_kwargs_on"] = existing
    merged["anticheat"] = anticheat
    return merged


def _packet_for_impact_kwarg_scan(
    packet: dict[str, Any], vendor_kwargs: dict[str, list[str]]
) -> dict[str, Any]:
    """Scan with vendor/anticheat bans only — not AST_PARAM_RENAME (apply covers those)."""
    rules = [
        r
        for r in (packet.get("rules") or [])
        if not (
            isinstance(r, dict)
            and str(r.get("type") or "") == "AST_PARAM_RENAME"
        )
    ]
    base = dict(packet)
    base["rules"] = rules
    return _packet_with_banned_kwargs(base, vendor_kwargs)


def mechanical_impact_pass(
    root: Path,
    packet: dict[str, Any],
    *,
    planned_paths: set[str],
    oracle_paths: set[str],
    allowlist_paths: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """
    Return (findings, post_rules, defer_paths).
    """
    root = root.resolve()
    findings: list[dict[str, Any]] = []
    post_rules: list[dict[str, Any]] = []
    defer_paths: set[str] = set()
    vendor_kwargs = default_banned_kwargs_on(packet)
    scan_packet = _packet_for_impact_kwarg_scan(packet, vendor_kwargs)

    scan_paths = set(planned_paths) | allowlist_paths | set(oracle_paths)
    for rel in sorted(scan_paths):
        rel = _posix(rel)
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        for finding in old_kwargs_on_new_callee_findings(text, rel, scan_packet):
            findings.append(
                {
                    "path": rel,
                    "kind": "legacy_kwargs_on_rewritten_callee",
                    "severity": "error",
                    "action": "fix",
                    "detail": finding,
                    "source": "mechanical",
                    "required": rel in oracle_paths or rel in planned_paths,
                }
            )

        for cls in impact_classes(packet):
            markers = cls.get("markers") or []
            if not _has_marker(text, markers):
                continue
            target_globs = cls.get("target_globs") or ["**/*"]
            if not _glob_ok(rel, target_globs):
                continue

            required_if = cls.get("required_if") or []
            on_oracle = "oracle_scan" in required_if and rel in oracle_paths
            on_plan = "planned_touch" in required_if and rel in planned_paths
            required = on_oracle or on_plan
            if not required and rel not in planned_paths:
                continue

            cls_id = str(cls.get("id") or "impact_class")
            fix = cls.get("fix") or {}
            if fix.get("advisory"):
                findings.append(
                    {
                        "path": rel,
                        "kind": cls_id,
                        "severity": "warning",
                        "action": "advisory",
                        "detail": f"Legacy response access pattern in {rel}",
                        "source": "mechanical",
                        "required": required,
                    }
                )
                continue

            if cls_id == "azure_classic_client":
                body = azure_bridge_module_body()
                forbidden = oracle_forbidden_tokens(packet)
                bad = [t for t in forbidden if token_in_text(body, t)]
                if bad:
                    findings.append(
                        {
                            "path": rel,
                            "kind": cls_id,
                            "severity": "error",
                            "action": "block",
                            "detail": f"Azure template contains forbidden tokens: {bad[:3]}",
                            "source": "mechanical",
                            "required": True,
                        }
                    )
                    continue
                rule = dict(fix.get("post_rule") or {})
                rule.setdefault("type", "FUNCTION_BODY_REPLACE")
                rule["body"] = body
                rule["target_files"] = [rel]
                rule["source"] = "impact:azure_classic_client"
                post_rules.append(rule)
                if fix.get("defer_during_apply"):
                    defer_paths.add(rel)
                findings.append(
                    {
                        "path": rel,
                        "kind": cls_id,
                        "severity": "error",
                        "action": "fix",
                        "detail": "Queue AzureOpenAI module migration before verify",
                        "source": "mechanical",
                        "required": True,
                    }
                )

    return findings, post_rules, defer_paths
