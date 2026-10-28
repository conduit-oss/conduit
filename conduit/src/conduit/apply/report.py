"""Human-readable apply report."""

from __future__ import annotations

from conduit.apply.engine import ApplyReport
from conduit.apply.models import SiteStatus


def format_report(report: ApplyReport, *, include_context: bool = False) -> str:
    if not report.results:
        return "No impact sites to apply.\n"
    lines: list[str] = []
    lines.append("site\tstatus\twhy")
    lines.append("----\t------\t---")
    for item in report.results:
        why = item.why or item.error or ""
        why = why.replace("\t", " ").replace("\n", " ")
        lines.append(f"{item.site.path}:{item.site.line}\t{item.status.value}\t{why}")
        if item.diff and item.status != SiteStatus.DRY_RUN:
            lines.append("")
            lines.append(item.diff.rstrip())
            lines.append("")
        if include_context and item.context is not None:
            pack = item.context
            lines.append(f"# context {pack.file_path}::{pack.function_name}")
            lines.append(f"# release_diff: {pack.release_diff.get('change')}")
            lines.append(pack.function_source.rstrip())
            lines.append("")
    if report.preexisting:
        lines.append(report.preexisting_note)
    lines.append(f"tokens_used: {report.tokens_used}")
    if report.flaky:
        lines.append("flaky, not ours: " + ", ".join(report.flaky))
    if report.trace_note:
        lines.append(report.trace_note)
    if report.retest_runs:
        lines.append(
            f"retests: {report.retest_runs} extra test run"
            f"{'' if report.retest_runs == 1 else 's'}, {report.retest_seconds:.2f}s"
        )
    return "\n".join(lines).rstrip() + "\n"


def model_call_lines(report: ApplyReport) -> list[str]:
    """One log line per model and thinking setting, then the run's total.

    Endpoint host and path, model, thinking on or off, calls and tokens; never
    a key or header."""
    lines = [
        f"Token Factory call: POST {item['endpoint']} model={item['model']} "
        f"thinking={'on' if item['enable_thinking'] else 'off'} calls={item['calls']} tokens={item['tokens']}"
        for item in report.model_calls
    ]
    if lines:
        lines.append(
            f"Token Factory total: calls={sum(item['calls'] for item in report.model_calls)} "
            f"tokens={report.tokens_used}"
        )
    return lines
