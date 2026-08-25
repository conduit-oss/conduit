"""Migration audit log: apply + repair deltas for the log-first LLM auditor."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from conduit.patcher.engine import PatchReport

_LOG_NAME = "migration_audit.jsonl"
_META_NAME = "migration_audit_meta.json"
_MAX_DIFF_CHARS = 2000
_MAX_LLM_ENTRIES = 200
_MAX_LLM_CHARS = 80_000

_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(OPENAI_API_KEY|CONDUIT_LLM_API_KEY|API_KEY|SECRET|TOKEN)\s*[=:]\s*\S+)"
)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    return _SECRET_RE.sub("[REDACTED]", text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _unified_snippet(
    before: str | None, after: str | None, *, limit: int = _MAX_DIFF_CHARS
) -> str:
    b_lines = (before or "").splitlines()
    a_lines = (after or "").splitlines()
    if len(before or "") + len(after or "") > limit * 2:
        return (
            f"+{len(a_lines)}/-{len(b_lines)} lines; "
            f"before_sha={_sha256(before or '')} after_sha={_sha256(after or '')}"
        )
    b_set = set(b_lines)
    a_set = set(a_lines)
    removed = [f"- {ln}" for ln in b_lines if ln not in a_set][:40]
    added = [f"+ {ln}" for ln in a_lines if ln not in b_set][:40]
    body = redact_secrets("\n".join(removed + added))
    if len(body) > limit:
        return body[:limit] + f"\n… truncated ({len(body)} chars)"
    return body


@dataclass
class MigrationAuditLog:
    """Append-only log of migration edits for anti-cheat audit."""

    header: dict[str, Any] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)
    score: dict[str, Any] | None = None
    _seq: int = 0

    @classmethod
    def from_packet(
        cls, packet: dict[str, Any], *, root: Path | None = None
    ) -> MigrationAuditLog:
        return cls(
            header={
                "packet_id": str(packet.get("packet_id") or ""),
                "package": str(packet.get("package") or ""),
                "ecosystem": str(packet.get("ecosystem") or ""),
                "from_version": str(packet.get("from_version") or ""),
                "to_version": str(packet.get("to_version") or ""),
                "root": str(root.resolve()) if root is not None else "",
            }
        )

    def _next_id(self) -> str:
        self._seq += 1
        return f"e{self._seq}"

    def paths(self) -> set[str]:
        out: set[str] = set()
        for entry in self.entries:
            path = str(entry.get("path") or "").replace("\\", "/")
            if path and path != "(packet)":
                out.add(path)
            for extra in entry.get("paths") or []:
                p = str(extra).replace("\\", "/")
                if p:
                    out.add(p)
        return out

    def record(
        self,
        phase: str,
        *,
        path: str = "",
        detail: str = "",
        **extra: Any,
    ) -> str:
        eid = self._next_id()
        entry: dict[str, Any] = {
            "id": eid,
            "phase": phase,
            "path": str(path or "").replace("\\", "/"),
            "detail": redact_secrets(str(detail or "")),
        }
        for key, val in extra.items():
            if val is None:
                continue
            if isinstance(val, str):
                entry[key] = redact_secrets(val)
            else:
                entry[key] = val
        self.entries.append(entry)
        return eid

    def record_apply(self, report: PatchReport) -> None:
        for change in report.changes:
            self.record(
                "apply",
                path=change.path,
                detail=change.detail,
                rule_type=change.rule_type,
                event_id=change.event_id,
            )
        for skip in report.skips:
            self.record("apply_skip", detail=str(skip))

    def record_impact(self, impact: Any) -> None:
        """Record pre-apply impact analysis findings."""
        blocked = bool(getattr(impact, "blocked", False))
        self.record(
            "impact",
            detail="blocked" if blocked else "ok",
            blocked=blocked,
            block_reason=str(getattr(impact, "block_reason", "") or ""),
            finding_count=len(getattr(impact, "findings", []) or []),
            post_rule_count=len(getattr(impact, "post_rules", []) or []),
            defer_paths=sorted(getattr(impact, "defer_paths", []) or []),
        )
        for finding in getattr(impact, "findings", []) or []:
            if not isinstance(finding, dict):
                continue
            self.record(
                "impact_finding",
                path=str(finding.get("path") or ""),
                detail=str(finding.get("detail") or finding.get("kind") or ""),
                action=str(finding.get("action") or ""),
                source=str(finding.get("source") or ""),
            )

    def record_generated(self, paths: Iterable[str]) -> None:
        for rel in paths:
            self.record("test_gen", path=str(rel), detail="generated conduit test")

    def record_write(
        self,
        path: str,
        *,
        source: str = "repair",
        attempt: int | None = None,
        before: str | None = None,
        after: str | None = None,
        detail: str = "",
    ) -> str:
        snippet = _unified_snippet(before, after)
        return self.record(
            "write",
            path=path,
            detail=detail or f"{source} wrote {path}",
            source=source,
            attempt=attempt,
            before_sha=_sha256(before or "") if before is not None else None,
            after_sha=_sha256(after or "") if after is not None else None,
            diff=snippet,
        )

    def record_reject(
        self,
        path: str,
        reason: str,
        *,
        source: str = "repair",
        attempt: int | None = None,
    ) -> str:
        return self.record(
            "reject",
            path=path,
            detail=reason,
            source=source,
            attempt=attempt,
        )

    def record_restore(
        self,
        paths: Iterable[str],
        *,
        attempt: int | None = None,
        detail: str = "",
    ) -> str:
        plist = [str(p).replace("\\", "/") for p in paths]
        return self.record(
            "restore",
            path=plist[0] if len(plist) == 1 else "",
            paths=plist,
            detail=detail or f"restored {len(plist)} file(s) after regression",
            attempt=attempt,
        )

    def record_mechanical(
        self, findings: Iterable[str], *, gate: str = ""
    ) -> str:
        msgs = [str(f) for f in findings]
        return self.record(
            "mechanical",
            detail=gate or "mechanical anti-cheat",
            findings=msgs,
            failed=bool(msgs),
        )

    def record_packet_patch(
        self, details: Iterable[str], *, attempt: int | None = None
    ) -> str:
        return self.record(
            "packet_patch",
            detail="; ".join(str(d) for d in details),
            attempt=attempt,
        )

    def persist(self, root: Path) -> Path:
        dest = root.resolve() / ".conduit"
        dest.mkdir(parents=True, exist_ok=True)
        meta = {**self.header, "entry_count": len(self.entries), "score": self.score}
        (dest / _META_NAME).write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        lines = [json.dumps(e, ensure_ascii=False) for e in self.entries]
        path = dest / _LOG_NAME
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    @classmethod
    def load(cls, root: Path) -> MigrationAuditLog | None:
        dest = root.resolve() / ".conduit"
        log_path = dest / _LOG_NAME
        meta_path = dest / _META_NAME
        if not log_path.is_file():
            return None
        header: dict[str, Any] = {}
        score = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    score = meta.get("score")
                    header = {
                        k: meta.get(k)
                        for k in (
                            "packet_id",
                            "package",
                            "ecosystem",
                            "from_version",
                            "to_version",
                            "root",
                        )
                        if k in meta
                    }
            except (OSError, json.JSONDecodeError):
                pass
        entries: list[dict[str, Any]] = []
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        except OSError:
            return None
        log = cls(
            header=header,
            entries=entries,
            score=score if isinstance(score, dict) else None,
        )
        log._seq = len(entries)
        return log

    def for_llm(
        self,
        *,
        max_entries: int = _MAX_LLM_ENTRIES,
        max_chars: int = _MAX_LLM_CHARS,
    ) -> dict[str, Any]:
        """Capped payload: prefer apply + rejects + latest writes."""
        apply_e = [
            e
            for e in self.entries
            if e.get("phase") in {"apply", "apply_skip", "test_gen"}
        ]
        reject_e = [e for e in self.entries if e.get("phase") == "reject"]
        mech_e = [e for e in self.entries if e.get("phase") == "mechanical"]
        other = [
            e
            for e in self.entries
            if e.get("phase")
            not in {"apply", "apply_skip", "test_gen", "reject", "mechanical"}
        ]
        other = other[-max(40, max_entries // 3) :]
        selected = apply_e + reject_e + mech_e[-3:] + other
        if len(selected) > max_entries:
            head = apply_e[: max_entries // 2]
            selected = (head + reject_e + mech_e[-2:] + other)[:max_entries]

        payload: dict[str, Any] = {
            "header": dict(self.header),
            "entries": selected,
            "path_allowlist": sorted(self.paths()),
        }
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) <= max_chars:
            return payload
        slim_entries = []
        for e in selected:
            copy = dict(e)
            if "diff" in copy and len(str(copy.get("diff") or "")) > 200:
                copy["diff"] = str(copy["diff"])[:200] + "…"
            slim_entries.append(copy)
        payload["entries"] = slim_entries
        raw = json.dumps(payload, ensure_ascii=False)
        while len(raw) > max_chars and payload["entries"]:
            payload["entries"].pop(0)
            raw = json.dumps(payload, ensure_ascii=False)
        payload["truncated"] = True
        return payload

    def repair_journal(
        self,
        *,
        attempt: int | None = None,
        max_entries: int = 24,
        max_chars: int = 8_000,
    ) -> dict[str, Any]:
        """Slim cross-attempt memory for the repair agent (not the full audit dump)."""
        phases = {"write", "reject", "restore", "packet_patch"}
        relevant = [e for e in self.entries if e.get("phase") in phases]
        if attempt is not None:
            prior = [
                e
                for e in relevant
                if e.get("attempt") is None or int(e.get("attempt") or 0) < attempt
            ]
            if prior:
                relevant = prior
        relevant = relevant[-max_entries:]
        entries: list[dict[str, Any]] = []
        for e in relevant:
            item: dict[str, Any] = {
                "phase": e.get("phase"),
                "path": e.get("path"),
                "detail": e.get("detail"),
                "attempt": e.get("attempt"),
            }
            if e.get("phase") == "reject":
                item["reason"] = e.get("detail")
            if e.get("phase") == "restore" and e.get("paths"):
                item["paths"] = e.get("paths")
            diff = e.get("diff")
            if isinstance(diff, str) and diff.strip():
                item["diff"] = diff[:400] + ("…" if len(diff) > 400 else "")
            entries.append(item)
        payload: dict[str, Any] = {"entries": entries}
        raw = json.dumps(payload, ensure_ascii=False)
        while len(raw) > max_chars and payload["entries"]:
            payload["entries"].pop(0)
            raw = json.dumps(payload, ensure_ascii=False)
            payload["truncated"] = True
        return payload

    def repair_journal(
        self,
        *,
        attempt: int | None = None,
        max_entries: int = 24,
        max_chars: int = 8_000,
    ) -> dict[str, Any]:
        """Slim cross-attempt memory for the repair agent (not the full audit dump)."""
        phases = {"write", "reject", "restore", "packet_patch"}
        relevant = [e for e in self.entries if e.get("phase") in phases]
        if attempt is not None:
            # Prefer entries from prior attempts, then any without attempt.
            prior = [
                e
                for e in relevant
                if e.get("attempt") is None or int(e.get("attempt") or 0) < attempt
            ]
            if prior:
                relevant = prior
        relevant = relevant[-max_entries:]
        entries: list[dict[str, Any]] = []
        for e in relevant:
            item = {
                "phase": e.get("phase"),
                "path": e.get("path"),
                "detail": e.get("detail"),
                "attempt": e.get("attempt"),
            }
            if e.get("phase") == "reject":
                item["reason"] = e.get("detail")
            if e.get("phase") == "restore" and e.get("paths"):
                item["paths"] = e.get("paths")
            diff = e.get("diff")
            if isinstance(diff, str) and diff.strip():
                item["diff"] = diff[:400] + ("…" if len(diff) > 400 else "")
            entries.append(item)
        payload: dict[str, Any] = {"entries": entries}
        raw = json.dumps(payload, ensure_ascii=False)
        while len(raw) > max_chars and payload["entries"]:
            payload["entries"].pop(0)
            raw = json.dumps(payload, ensure_ascii=False)
            payload["truncated"] = True
        return payload
