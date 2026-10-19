"""Trim verification errors before they are fed back to the model.

``TEST_FEEDBACK_MAX_LINES`` caps how much test-command output is resent on
retry. Prefer the trailing lines (failure summaries usually land at the end).
"""

from __future__ import annotations

# Max lines of test-command stdout/stderr included in the next propose prompt.
TEST_FEEDBACK_MAX_LINES = 40


def trim_test_feedback(
    text: str, *, max_lines: int = TEST_FEEDBACK_MAX_LINES
) -> str:
    """Return the last ``max_lines`` lines of ``text`` (stripped)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    lines = cleaned.splitlines()
    if len(lines) <= max_lines:
        return cleaned
    omitted = len(lines) - max_lines
    body = "\n".join(lines[-max_lines:])
    return f"… ({omitted} earlier lines omitted)\n{body}"
