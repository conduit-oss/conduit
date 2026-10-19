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
    lines.append(f"tokens_used: {report.tokens_used}")
    return "\n".join(lines).rstrip() + "\n"
