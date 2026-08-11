"""Rate-limit / retry helpers for LLM provider calls."""

from __future__ import annotations

import re
import time
from typing import Callable, TypeVar

T = TypeVar("T")

_RETRY_AFTER_RE = re.compile(
    r"try again in\s+(?P<secs>\d+(?:\.\d+)?)\s*s",
    re.I,
)


def is_rate_limit_error(exc: BaseException) -> bool:
    """True when the exception looks like an HTTP 429 / rate limit."""
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg


def parse_retry_after_seconds(exc: BaseException, *, default: float = 2.0) -> float:
    """Parse provider 'try again in Xs' hints; fall back to default."""
    msg = str(exc)
    m = _RETRY_AFTER_RE.search(msg)
    if m:
        try:
            return max(0.5, float(m.group("secs")))
        except ValueError:
            pass
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            try:
                return max(0.5, float(raw))
            except (TypeError, ValueError):
                pass
    return max(0.5, default)


def call_with_rate_limit_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 4,
    log: Callable[[str], None] | None = None,
) -> T:
    """
    Call ``fn``; on rate-limit errors sleep and retry with backoff.

    Honour explicit 'try again in Xs' when present; otherwise exponential backoff.
    """
    emit = log or (lambda _m: None)
    last_exc: BaseException | None = None
    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt >= attempts:
                raise
            last_exc = exc
            wait = parse_retry_after_seconds(exc, default=min(60.0, 2.0**attempt))
            emit(
                f"[llm] rate limited (attempt {attempt}/{attempts}); "
                f"sleeping {wait:.1f}s then retrying"
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc
