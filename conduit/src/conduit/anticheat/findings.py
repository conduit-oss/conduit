"""Structured anti-cheat findings with severity tiers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from conduit.anticheat.vendor import cheat_kind_severity

_MESSAGE_RE = re.compile(
    r"^(?P<path>[^:\n]+):\s*(?P<kind>[^—\n]+)\s*—\s*(?P<detail>.+?)"
    r"(?:\s*\[(?P<log_ref>[^\]]+)\])?\s*$"
)

_KIND_ALIASES = {
    "compat": "compat_wrapper",
    "compatwrapper": "compat_wrapper",
    "fake_sdk": "fake_client",
    "incomplete": "incomplete_migration",
    "echo_stub": "echo_script_stub",
}


def normalize_kind(kind: str) -> str:
    k = (kind or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    return _KIND_ALIASES.get(k, k)


def infer_kind_from_message(message: str) -> str:
    lower = (message or "").lower()
    if "echo-stubs" in lower or "echo_stub" in lower:
        return "echo_script_stub"
    if "http paths without importing" in lower or "parallel http" in lower:
        return "parallel_http_without_sdk"
    if "still uses" in lower and "=" in lower and "migrate" in lower:
        return "legacy_kwargs_on_rewritten_callee"
    if "dropped official" in lower and "import" in lower:
        return "dropped_sdk_import"
    if "obfuscates leftover" in lower:
        return "obfuscated_tokens"
    if "synthetic" in lower or "dummy except" in lower:
        return "synthetic_except"
    if "compat" in lower or "fake" in lower and "client" in lower:
        return "compat_wrapper"
    if "incomplete" in lower or "ad hoc" in lower:
        return "incomplete_migration"
    return "mechanical"


@dataclass
class AnticheatFinding:
    path: str = ""
    kind: str = "unknown"
    detail: str = ""
    severity: str = "block"
    source: str = "mechanical"
    log_ref: str = ""

    def to_message(self) -> str:
        path = self.path.strip()
        kind = self.kind.strip() or "unknown"
        detail = self.detail.strip() or kind
        msg = f"{path}: {kind} — {detail}" if path else f"{kind} — {detail}"
        if self.log_ref:
            msg += f" [{self.log_ref}]"
        return msg

    @classmethod
    def from_message(
        cls,
        message: str,
        *,
        packet: dict[str, Any] | None = None,
        source: str = "mechanical",
        severity: str | None = None,
    ) -> AnticheatFinding:
        text = (message or "").strip()
        m = _MESSAGE_RE.match(text)
        if m:
            path = m.group("path").strip().replace("\\", "/")
            kind = normalize_kind(m.group("kind"))
            detail = m.group("detail").strip()
            log_ref = (m.group("log_ref") or "").strip()
        elif ":" in text:
            path, _, detail = text.partition(":")
            path = path.strip().replace("\\", "/")
            kind = infer_kind_from_message(detail)
            detail = detail.strip()
            log_ref = ""
        else:
            path = ""
            kind = infer_kind_from_message(text)
            detail = text
            log_ref = ""
        sev = severity or cheat_kind_severity(packet or {}, kind)
        return cls(
            path=path,
            kind=kind,
            detail=detail,
            severity=sev,
            source=source,
            log_ref=log_ref,
        )

    @classmethod
    def from_llm_dict(
        cls, item: dict[str, Any], *, packet: dict[str, Any]
    ) -> AnticheatFinding:
        path = str(item.get("path") or "").strip().replace("\\", "/")
        kind = normalize_kind(str(item.get("kind") or "cheat"))
        detail = str(item.get("detail") or kind).strip()
        log_ref = str(item.get("log_ref") or "").strip()
        raw_sev = str(item.get("severity") or "").strip().lower()
        if raw_sev in {"advisory", "warn", "warning"}:
            severity = "advisory"
        elif raw_sev == "block":
            severity = "block"
        else:
            severity = cheat_kind_severity(packet, kind)
        return cls(
            path=path,
            kind=kind,
            detail=detail,
            severity=severity,
            source="llm",
            log_ref=log_ref,
        )


def classify_mechanical_messages(
    messages: list[str], *, packet: dict[str, Any]
) -> list[AnticheatFinding]:
    out: list[AnticheatFinding] = []
    seen: set[str] = set()
    for msg in messages:
        if not msg or msg in seen:
            continue
        seen.add(msg)
        finding = AnticheatFinding.from_message(msg, packet=packet, source="mechanical")
        out.append(finding)
    return out


def parse_anticheat_failure_blob(text: str) -> list[AnticheatFinding]:
    """Parse anticheat failed stdout into structured findings."""
    findings: list[AnticheatFinding] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("anticheat failed"):
            continue
        if ":" not in line:
            continue
        findings.append(AnticheatFinding.from_message(line, source="mixed"))
    return findings
