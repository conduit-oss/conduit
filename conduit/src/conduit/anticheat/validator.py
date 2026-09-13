"""Validate LLM anti-cheat proposals before they fail verify."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from conduit.anticheat.findings import AnticheatFinding
from conduit.anticheat.legal_surfaces import COMPAT_WRAPPER_KINDS, matches_legal_surface
from conduit.anticheat.scope import audit_scope_paths
from conduit.anticheat.vendor import cheat_kind_severity
from conduit.repair_ignore import IgnoreList, build_ignore_list


def _advisory_mode() -> bool:
    return os.environ.get("CONDUIT_ANTICHEAT_ADVISORY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _audit_log_has_ref(audit_log: Any, log_ref: str) -> bool:
    if not log_ref or audit_log is None:
        return False
    ref = log_ref.strip().lower()
    for entry in getattr(audit_log, "entries", []) or []:
        eid = str(entry.get("id") or "").strip().lower()
        if eid == ref or ref in eid:
            return True
    return False


def validate_llm_cheats(
    cheats: list[AnticheatFinding],
    *,
    root: Path,
    packet: dict[str, Any],
    audit_log: Any | None = None,
    ignore: IgnoreList | None = None,
    mechanical_block: list[AnticheatFinding] | None = None,
    log: Any = None,
) -> tuple[list[AnticheatFinding], list[AnticheatFinding]]:
    """
    Return (block_findings, advisory_findings) after validation/filtering.
    """
    emit = log if callable(log) else None
    ignore = ignore or build_ignore_list(root, packet)
    scope = audit_scope_paths(
        root,
        packet,
        block_findings=mechanical_block,
        ignore=ignore,
    )
    block: list[AnticheatFinding] = []
    advisory: list[AnticheatFinding] = []

    for cheat in cheats:
        item = cheat
        if _advisory_mode():
            item = AnticheatFinding(
                path=item.path,
                kind=item.kind,
                detail=item.detail,
                severity="advisory",
                source=item.source,
                log_ref=item.log_ref,
            )

        path = item.path.replace("\\", "/")
        kind = item.kind.lower()

        if path and scope and path not in scope:
            if emit:
                emit(f"[anticheat] dropped out-of-scope cheat: {path}")
            advisory.append(
                AnticheatFinding(
                    path=path,
                    kind=kind,
                    detail=f"out of audit scope: {item.detail}",
                    severity="advisory",
                    source="llm",
                    log_ref=item.log_ref,
                )
            )
            continue

        if kind in COMPAT_WRAPPER_KINDS or "compat" in kind:
            file_path = root / path if path else None
            text = ""
            if file_path and file_path.is_file():
                try:
                    text = file_path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
            if text and matches_legal_surface(text, packet):
                if emit:
                    emit(
                        f"[anticheat] dropped compat_wrapper on official SDK surface: "
                        f"{path}"
                    )
                continue

        if item.severity == "block" and item.log_ref and not _audit_log_has_ref(
            audit_log, item.log_ref
        ):
            if emit:
                emit(
                    f"[anticheat] downgraded cheat missing log_ref {item.log_ref!r}: "
                    f"{path}"
                )
            advisory.append(
                AnticheatFinding(
                    path=path,
                    kind=kind,
                    detail=item.detail,
                    severity="advisory",
                    source="llm",
                    log_ref=item.log_ref,
                )
            )
            continue

        if cheat_kind_severity(packet, kind) == "advisory":
            item = AnticheatFinding(
                path=path,
                kind=kind,
                detail=item.detail,
                severity="advisory",
                source="llm",
                log_ref=item.log_ref,
            )

        if item.severity == "block":
            block.append(item)
        else:
            advisory.append(item)

    return block, advisory
