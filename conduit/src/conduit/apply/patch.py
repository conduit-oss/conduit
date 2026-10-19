"""Parse, validate, and apply unified diffs scoped to one function (+ imports)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from conduit.apply.models import ContextPack

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


class PatchError(ValueError):
    pass


def extract_unified_diff(text: str) -> str:
    """Pull a unified diff out of a model reply (fenced or raw)."""
    fenced = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    body = fenced.group(1) if fenced else text
    lines = body.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith(("--- ", "diff --git", "@@ ")):
            start = i
            break
    out = "\n".join(lines[start:]).strip()
    if not out:
        raise PatchError("model reply contains no unified diff")
    return out + "\n"


def parse_hunks(diff_text: str) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    current: list[str] | None = None
    old_start = old_count = new_start = new_count = 0
    for line in diff_text.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            if current is not None:
                hunks.append(
                    DiffHunk(old_start, old_count, new_start, new_count, tuple(current))
                )
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            current = []
            continue
        if current is None:
            continue
        if line.startswith(("+", "-", " ", "\\")):
            current.append(line)
    if current is not None:
        hunks.append(DiffHunk(old_start, old_count, new_start, new_count, tuple(current)))
    if not hunks:
        raise PatchError("diff has no hunks")
    return hunks


def validate_diff_scope(diff_text: str, pack: ContextPack) -> None:
    """Reject diffs that touch lines outside the flagged function (imports ok)."""
    hunks = parse_hunks(diff_text)
    import_lines = _import_line_set(pack.full_source)
    func_start = pack.function_start_line
    func_end = pack.function_end_line
    for hunk in hunks:
        if not _hunk_in_allowed_region(hunk, func_start, func_end, import_lines):
            raise PatchError(
                f"hunk at old line {hunk.old_start} is outside the flagged function "
                f"({func_start}-{func_end}) and imports"
            )
        old_line = hunk.old_start
        for raw in hunk.lines:
            if raw.startswith("\\"):
                continue
            if raw.startswith((" ", "-")):
                _assert_allowed(old_line, func_start, func_end, import_lines)
                old_line += 1


def _hunk_in_allowed_region(
    hunk: DiffHunk, func_start: int, func_end: int, import_lines: set[int]
) -> bool:
    for line in range(hunk.old_start, hunk.old_start + max(hunk.old_count, 1)):
        if func_start <= line <= func_end or line in import_lines:
            return True
    # Pure insertions at the function boundary: old_count may be 0.
    return func_start <= hunk.old_start <= func_end + 1 or hunk.old_start in import_lines


def _assert_allowed(
    line: int, func_start: int, func_end: int, import_lines: set[int]
) -> None:
    if func_start <= line <= func_end or line in import_lines:
        return
    raise PatchError(
        f"diff touches line {line}, outside flagged function "
        f"({func_start}-{func_end}) and module imports"
    )


def _import_line_set(source: str) -> set[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start = node.lineno
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            lines.update(range(start, end + 1))
    return lines


def apply_unified_diff(source: str, diff_text: str) -> str:
    """Apply a single-file unified diff to ``source``. Raises ``PatchError`` on mismatch."""
    hunks = parse_hunks(diff_text)
    base = source.splitlines(keepends=True)
    # Ensure every line has a trailing newline for stable patch matching.
    had_trailing = source.endswith("\n") or source == ""
    normalized: list[str] = []
    for line in base:
        normalized.append(line if line.endswith("\n") else line + "\n")
    base = normalized
    offset = 0
    for hunk in hunks:
        abs_start = hunk.old_start - 1 + offset
        if abs_start < 0 or abs_start > len(base):
            raise PatchError(f"hunk start {hunk.old_start} is out of range")
        rebuilt = list(base[:abs_start])
        cursor = abs_start
        old_consumed = 0
        new_added = 0
        for raw in hunk.lines:
            if raw.startswith("\\"):
                continue
            tag = raw[:1]
            content = raw[1:]
            expected = content + "\n"
            if tag == " ":
                if cursor >= len(base) or base[cursor] != expected:
                    raise PatchError(
                        f"context mismatch at line {cursor + 1}: "
                        f"expected {content!r}, got {_peek(base, cursor)!r}"
                    )
                rebuilt.append(base[cursor])
                cursor += 1
                old_consumed += 1
            elif tag == "-":
                if cursor >= len(base) or base[cursor] != expected:
                    raise PatchError(
                        f"delete mismatch at line {cursor + 1}: "
                        f"expected {content!r}, got {_peek(base, cursor)!r}"
                    )
                cursor += 1
                old_consumed += 1
            elif tag == "+":
                rebuilt.append(expected)
                new_added += 1
            else:
                raise PatchError(f"bad hunk line: {raw!r}")
        rebuilt.extend(base[cursor:])
        offset += new_added - old_consumed
        base = rebuilt
    text = "".join(base)
    if not had_trailing and text.endswith("\n"):
        text = text[:-1]
    return text


def _peek(lines: list[str], index: int) -> str:
    if index >= len(lines):
        return "<EOF>"
    line = lines[index]
    return line[:-1] if line.endswith("\n") else line


def ensure_parses(source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise PatchError(f"patched source does not parse: {exc}") from exc
