"""Precise byte-range source edits (tree-sitter friendly)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class SourceEdit:
    """A replacement of ``source[start_byte:end_byte]`` with ``text``."""

    start_byte: int
    end_byte: int
    text: str

    def __post_init__(self) -> None:
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError(
                f"invalid edit range [{self.start_byte}, {self.end_byte})"
            )


def apply_edits(source: str | bytes, edits: list[SourceEdit]) -> tuple[str, int]:
    """Apply edits back-to-front so earlier offsets stay valid.

    Returns ``(updated_text, number_of_edits_applied)``.
    Overlapping edits are applied in descending start order; later
    (lower-offset) edits that overlap an already-applied span are skipped.
    """
    if not edits:
        text = source.decode("utf-8") if isinstance(source, bytes) else source
        return text, 0

    raw = source.encode("utf-8") if isinstance(source, str) else source
    ordered = sorted(edits, key=lambda e: (e.start_byte, e.end_byte), reverse=True)
    applied = 0
    last_start: int | None = None
    for edit in ordered:
        if last_start is not None and edit.end_byte > last_start:
            continue  # overlaps a higher-offset edit already applied
        raw = raw[: edit.start_byte] + edit.text.encode("utf-8") + raw[edit.end_byte :]
        applied += 1
        last_start = edit.start_byte
    return raw.decode("utf-8"), applied
