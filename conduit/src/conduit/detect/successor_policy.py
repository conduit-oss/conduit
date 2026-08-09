"""Prefer intermediate migration steps over jumping to the newest successor.

Vendor modules should use this when choosing among multiple replacements
(models, endpoints, package majors, etc.). Documented chain order wins;
otherwise pick the nearest step up from the legacy id — never “newest first.”
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence


def parse_successor_chain(cell: str) -> list[str]:
    """
    Extract an ordered list of concrete successor ids from a docs cell.

    Order is preserved (first = nearest / recommended intermediate).
    """
    if not cell or not str(cell).strip():
        return []
    text = str(cell)
    # Split common list separators while keeping path-like and model-like tokens
    parts = re.split(r"(?:,|/|\bor\b|\band\b|;|\||\n)+", text, flags=re.I)
    out: list[str] = []
    seen: set[str] = set()
    token_re = re.compile(
        r"(gpt-[a-z0-9._-]+|o[0-9][a-z0-9._-]*|text-[a-z0-9._-]+|"
        r"dall-e-[0-9]|whisper-[a-z0-9._-]+|tts-[a-z0-9._-]+|"
        r"/v1/[a-z0-9/_-]+|[a-z][a-z0-9._-]{2,})",
        re.I,
    )
    for part in parts:
        part = part.strip().strip("`\"'")
        if not part:
            continue
        for m in token_re.finditer(part):
            tok = m.group(0).rstrip("*.,)")
            key = tok.lower()
            # Skip prose crumbs
            if key in {"see", "docs", "the", "and", "or", "to", "with", "for"}:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(tok)
    return out


def pick_documented_step(cell: str) -> str | None:
    """First (intermediate) concrete successor from a replacement cell."""
    chain = parse_successor_chain(cell)
    return chain[0] if chain else None


# Approximate generation ladder for common model id families (lower = older).
_TIER_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^(?:ft-)?(?:ada|babbage|curie|davinci|text-davinci)", re.I), 10),
    (re.compile(r"^(?:ft-)?gpt-3\.5", re.I), 20),
    (re.compile(r"^(?:ft-)?gpt-4(?![o.1])", re.I), 30),
    (re.compile(r"^(?:ft-)?gpt-4o", re.I), 40),
    (re.compile(r"^(?:ft-)?gpt-4\.1", re.I), 41),
    (re.compile(r"^o1", re.I), 42),
    (re.compile(r"^o3", re.I), 43),
    (re.compile(r"^o4", re.I), 44),
    (re.compile(r"^(?:ft-)?gpt-5(?!\.)", re.I), 50),
    (re.compile(r"^(?:ft-)?gpt-5\.1", re.I), 51),
    (re.compile(r"^(?:ft-)?gpt-5\.2", re.I), 52),
    (re.compile(r"^(?:ft-)?gpt-5\.3", re.I), 53),
    (re.compile(r"^(?:ft-)?gpt-5\.4", re.I), 54),
    (re.compile(r"^(?:ft-)?gpt-5\.5", re.I), 55),
    (re.compile(r"^(?:ft-)?gpt-5\.6", re.I), 56),
]


def generation_tier(identifier: str | None) -> int | None:
    """Return a rough generation tier, or None if unknown."""
    if not identifier:
        return None
    text = identifier.strip()
    for pattern, tier in _TIER_PATTERNS:
        if pattern.search(text):
            return tier
    # Paths / other ids: no tier
    if text.startswith("/"):
        return None
    return None


def step_distance(from_id: str | None, to_id: str | None) -> int:
    """
    How far `to_id` is above `from_id` on the generation ladder.

    Prefer small positive steps. Unknown tiers sort after known ones.
    """
    a = generation_tier(from_id)
    b = generation_tier(to_id)
    if a is None or b is None:
        return 10_000
    delta = b - a
    if delta <= 0:
        # Same or older — poor successor
        return 5_000 + abs(delta)
    return delta


def rank_successors(
    candidates: Iterable[str],
    *,
    from_id: str | None = None,
    documented: Sequence[str] | None = None,
    reject: Sequence[str] | None = None,
    limit: int = 8,
) -> list[str]:
    """
    Rank successor candidates for a migration step.

    1. Documented chain order (first = intermediate step) — highest priority
    2. Remaining candidates by nearest generation step up from `from_id`
    3. Stable name tie-break (never “newest first”)
    """
    rejected = {r.lower() for r in (reject or []) if r}
    if from_id:
        rejected.add(from_id.lower())

    seen: set[str] = set()
    ordered: list[str] = []

    for mid in documented or []:
        if not mid or mid.lower() in rejected or mid.lower() in seen:
            continue
        seen.add(mid.lower())
        ordered.append(mid)

    rest: list[tuple[int, str, str]] = []
    for mid in candidates:
        if not mid or mid.lower() in rejected or mid.lower() in seen:
            continue
        seen.add(mid.lower())
        dist = step_distance(from_id, mid)
        rest.append((dist, mid.lower(), mid))
    rest.sort(key=lambda t: (t[0], t[1]))
    for _dist, _lower, mid in rest:
        ordered.append(mid)

    return ordered[:limit]


def prefer_intermediate_reason(
    *,
    from_id: str,
    chosen: str,
    documented: Sequence[str] | None = None,
) -> str:
    """Short reason fragment explaining intermediate-step preference."""
    doc = list(documented or [])
    if doc and chosen.lower() == doc[0].lower():
        return (
            f"Preferred intermediate successor {chosen} (first documented step "
            f"from {from_id}; chain={doc})."
        )
    if doc and chosen.lower() in {d.lower() for d in doc}:
        return (
            f"Preferred documented successor {chosen} for {from_id} "
            f"(not jumping past intermediate steps; chain={doc})."
        )
    return (
        f"Preferred nearest available step {chosen} from {from_id} "
        f"(avoid jumping to newest tier when a closer successor exists)."
    )
