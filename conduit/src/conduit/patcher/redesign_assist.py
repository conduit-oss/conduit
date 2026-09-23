"""Opt-in redesign assist for multi-step declaration refuse residuals.

Packets keep ``no_successor`` / ``multi_step`` honesty. This lane may invent a
redesign only when ``--assist-redesign`` is set. Good-to-go is leftover rescan
(+ anticheat when available), not silent success from the propose step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from conduit.patcher.leftovers import Leftover, scan_packet_leftovers

# Redesign-class: multi-step kwargs / no_successor options — not uncodable deletes.
_REDESIGN_MARKERS = (
    "each_item",
    "always",
    "decorated_def_options",
    "no_successor",
    "multi_step",
)
_UNCODABLE_MARKERS = (
    "uncodable",
    "MAX_EMAIL_LENGTH",
    "no successor id",
    "no rename match",
)


class RedesignProposer(Protocol):
    def __call__(
        self,
        *,
        root: Path,
        leftover: Leftover,
        source: str,
    ) -> str | None:
        """Return full new file text for leftover.rel, or None to skip."""


@dataclass
class RedesignAssistResult:
    attempted: bool
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    leftovers_after: tuple[Leftover, ...] = ()
    message: str = ""


def is_redesign_leftover(item: Leftover) -> bool:
    blob = f"{item.callee} {item.reason}".lower()
    if any(m.lower() in blob for m in _UNCODABLE_MARKERS):
        return False
    return any(m.lower() in blob for m in _REDESIGN_MARKERS)


def collect_redesign_leftovers(items: Sequence[Leftover]) -> list[Leftover]:
    return [i for i in items if is_redesign_leftover(i)]


def _safe_rel(root: Path, rel: str) -> Path | None:
    text = (rel or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in text.split("/"):
        return None
    path = (root / text).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def default_llm_proposer(*, client: Any) -> RedesignProposer:
    def propose(*, root: Path, leftover: Leftover, source: str) -> str | None:
        system = (
            "You redesign Python code for a pydantic v1→v2 migration gap. "
            "The site cannot be renamed 1:1 (e.g. each_item=True / always=True). "
            "Return JSON {\"content\": \"<full new file source>\"} only. "
            "Remove the unmappable decorator kwarg and apply a minimal v2-valid "
            "redesign. Do not invent unrelated public symbols. Preserve behavior "
            "when possible."
        )
        user = json.dumps(
            {
                "leftover": leftover.display(),
                "rel": leftover.rel,
                "source": source,
            },
            indent=2,
        )
        data = client.complete_json(system=system, user=user)
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        return content if isinstance(content, str) and content.strip() else None

    return propose


def run_redesign_assist(
    root: Path,
    packet: dict[str, Any],
    leftovers: Sequence[Leftover],
    *,
    propose: RedesignProposer,
) -> RedesignAssistResult:
    targets = collect_redesign_leftovers(leftovers)
    if not targets:
        return RedesignAssistResult(
            attempted=False,
            message="no redesign-class leftovers",
            leftovers_after=tuple(leftovers),
        )

    result = RedesignAssistResult(attempted=True)
    root = root.resolve()
    for item in targets:
        path = _safe_rel(root, item.rel)
        if path is None:
            result.skipped.append(item.rel)
            result.errors.append(f"unsafe or missing path: {item.rel}")
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            result.errors.append(f"{item.rel}: {exc}")
            result.skipped.append(item.rel)
            continue
        try:
            updated = propose(root=root, leftover=item, source=original)
        except Exception as exc:
            result.errors.append(f"{item.rel}: propose failed: {exc}")
            result.skipped.append(item.rel)
            continue
        if not updated or updated == original:
            result.skipped.append(item.rel)
            result.errors.append(f"{item.rel}: propose returned no change")
            continue
        # Refuse writing if propose clearly looks like deleting the whole module
        if len(updated.strip()) < 8:
            result.skipped.append(item.rel)
            result.errors.append(f"{item.rel}: propose returned empty/tiny content")
            continue
        path.write_text(updated, encoding="utf-8")
        result.applied.append(item.rel)

    after = scan_packet_leftovers(root, packet)
    result.leftovers_after = tuple(after)
    still = collect_redesign_leftovers(after)
    if still:
        result.message = (
            f"assist applied {len(result.applied)} file(s); "
            f"{len(still)} redesign leftover(s) remain"
        )
    else:
        result.message = (
            f"assist applied {len(result.applied)} file(s); "
            "redesign-class leftovers cleared"
        )
    return result


_EACH_ITEM_ARG = re.compile(
    r",\s*each_item\s*=\s*True\b|,?\s*each_item\s*=\s*True\s*,?"
)
_ALWAYS_ARG = re.compile(
    r",\s*always\s*=\s*True\b|,?\s*always\s*=\s*True\s*,?"
)


def deterministic_drop_unmapped_kwargs(
    *, root: Path, leftover: Leftover, source: str
) -> str | None:
    """Test/stub proposer: drop each_item=/always= kwargs only (minimal redesign)."""
    blob = f"{leftover.callee} {leftover.reason}"
    out = source
    if "each_item" in blob:
        out = _EACH_ITEM_ARG.sub("", out)
    if "always" in blob and "each_item" not in blob:
        out = _ALWAYS_ARG.sub("", out)
    # tidy double commas / trailing commas in decorator calls
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\(\s*,", "(", out)
    out = re.sub(r",\s*\)", ")", out)
    return out if out != source else None
