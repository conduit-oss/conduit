"""Pull old-name → new-name pairs out of release notes and changelogs."""

from __future__ import annotations

import re

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_DOTTED = rf"{_IDENT}(?:\.{_IDENT})+"

_CALL_RENAME = re.compile(
    rf"(?<![\w.])(?P<old>{_DOTTED})\s+(?:has been\s+)?renamed to\s+(?P<new>{_DOTTED})(?![\w])",
    re.IGNORECASE,
)
_PARAM_RENAME = re.compile(
    rf"(?<![\w.])(?P<old>{_IDENT})\s+(?:parameter\s+)?(?:has been\s+)?renamed to\s+(?P<new>{_IDENT})(?![\w])",
    re.IGNORECASE,
)


def plain_text(value: str) -> str:
    """Drop HTML tags when a feed body is markup."""
    text = value or ""
    if "<" not in text or ">" not in text:
        return text.replace("`", "")
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(text, "html.parser").get_text("\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
    return text.replace("`", "")


def renames_from_text(value: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(call_renames, param_renames)`` as ``(old, new)`` pairs.

    Dotted names are call renames. A bare identifier is a parameter rename.
    A parameter match that sits inside a dotted rename is ignored.
    """
    text = plain_text(value)
    calls: list[tuple[str, str]] = []
    covered: list[tuple[int, int]] = []
    for match in _CALL_RENAME.finditer(text):
        old, new = match.group("old"), match.group("new")
        if old == new:
            continue
        pair = (old, new)
        if pair not in calls:
            calls.append(pair)
        covered.append(match.span())

    params: list[tuple[str, str]] = []
    for match in _PARAM_RENAME.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        old, new = match.group("old"), match.group("new")
        if old == new:
            continue
        pair = (old, new)
        if pair not in params:
            params.append(pair)
    return calls, params


def rename_grounded(old: str, new: str, names: set[str]) -> bool:
    """True when both sides are identifiers present in ``names``.

    ``names`` is the public API of the old package, the new package, or both.
    A changelog sentence that merely looks like a rename is not accepted.
    """
    if not names:
        return False
    return bool(_name_tokens(old) & names) and bool(_name_tokens(new) & names)


def _name_tokens(value: str) -> set[str]:
    text = (value or "").strip()
    if not text:
        return set()
    parts = [part for part in text.split(".") if part]
    found = {text, parts[-1]}
    if len(parts) >= 2:
        found.add(f"{parts[-2]}.{parts[-1]}")
    return found
