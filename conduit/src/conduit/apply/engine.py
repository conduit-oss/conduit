"""Propose → splice → verify loop for each impact site."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from conduit.apply.client import ChatMessage, ModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.context import ContextError, build_context_pack, enclosing_function
from conduit.apply.feedback import trim_test_feedback
from conduit.apply.models import ContextPack, ImpactSite, SiteResult, SiteStatus
from conduit.apply.prompts import propose_messages, summarize_messages
from conduit.apply.splice import PatchError, parse_model_edit, splice_edit_with_span, unified_diff_for
from conduit.apply.signatures import new_release_wheel, signatures_for
from conduit.apply.typecheck import FLOWS
from conduit.apply.verify import remaining_breaks

# Optional hook: given root + site + patched file text, return True if the site
# is no longer flagged. Unit tests inject this; the CLI uses impact re-check.
SiteClearedCheck = Callable[[Path, ImpactSite, str], bool]


@dataclass
class _RouteState:
    """Accepted batch rewrites, and recheck findings that belong to one of them.

    A masked or missing attribute read inside a helper is reported on the site
    being checked. When that read sits in another site's rewrite, it is that
    rewrite's finding: the site is retried with the text, and it is not verified
    while the read is still in its rewrite.
    """

    accepted: dict[int, dict[str, Any]]
    key: int = 0
    pending: dict[int, list[str]] = field(default_factory=dict)
    traced: dict[int, list[str]] = field(default_factory=dict)
    #: Sites not rewritten yet (key -> site, its original line text). A finding in
    #: one of their functions is that site's to fix, not the producer's.
    upcoming: dict[int, tuple[ImpactSite, str]] = field(default_factory=dict)
    #: Findings handed to an upcoming site; they go into its prompt.
    deferred: dict[int, list[str]] = field(default_factory=dict)
    #: Sites already given their one final-recheck retry.
    retried: set[int] = field(default_factory=set)
    #: key -> keys of the sites whose function calls this site's function.
    feeders: dict[int, list[int]] = field(default_factory=dict)


@dataclass
class ApplyConfig:
    root: Path
    settings: TokenFactorySettings
    dry_run: bool = False
    write: bool = False
    test_cmd: str | None = None
    client: ModelClient | None = None
    site_cleared: SiteClearedCheck | None = None
    #: When True and ``site_cleared`` is unset, re-run ``collect_impacts`` on a
    #: scratch copy of the tree with the patch applied.
    recheck_impact: bool = False
    fixtures: Path | None = None
    allow_network: bool = False
    max_sites: int | None = None
    #: Batch only: cross-site recheck findings routed back to the rewrite that contains them.
    routes: _RouteState | None = None


@dataclass
class ApplyReport:
    results: list[SiteResult] = field(default_factory=list)
    tokens_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "tokens_used": self.tokens_used,
            "summary": {
                status.value: sum(1 for r in self.results if r.status == status)
                for status in SiteStatus
            },
        }


def apply_sites(sites: list[ImpactSite], config: ApplyConfig) -> ApplyReport:
    report = ApplyReport()
    client = config.client
    owns_client = False
    if client is None and not config.dry_run:
        from conduit.apply.client import TokenFactoryClient

        client = TokenFactoryClient(
            base_url=config.settings.base_url,
            api_key=config.settings.api_key,
        )
        owns_client = True
    try:
        selected = sites if config.max_sites is None else sites[: config.max_sites]
        if config.test_cmd and not config.dry_run and client is not None:
            _apply_batch(selected, config, client, report)
            _fill_why(report)
            return report
        covered, texts = _one_per_function(config.root, selected)
        # Lines that import or construct a removed class go first, so later
        # rechecks resolve calls through the new client instead of the old one.
        for site in sorted(selected, key=lambda item: not _names_removed_class(item)):
            if id(site) in covered:
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=covered[id(site)]))
                continue
            moved = _relocate(config.root, site, texts.get(id(site), "")) if config.write else site
            if moved is None:
                why = "line rewritten by an earlier fix in this file"
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=why))
                continue
            report.results.append(_apply_one(moved, config, client, report))
        _fill_why(report)
    finally:
        if owns_client and client is not None and hasattr(client, "close"):
            client.close()  # type: ignore[union-attr]
    return report


def _apply_batch(selected: list[ImpactSite], config: ApplyConfig, client: ModelClient, report: ApplyReport) -> None:
    """With a test command: rewrite every site (each must pass the static recheck),
    run the tests once, retry only the sites the failures point at, and revert
    the ones that cannot be cleared. A masked or missing attribute read inside
    another site's rewrite is retried on that rewrite, with the finding as
    feedback, before the tests run. Tests see the whole migration at once, so
    an import every module needs no longer fails each site on its own."""
    with tempfile.TemporaryDirectory(prefix="conduit-apply-batch-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(config.root, scratch, ignore=shutil.ignore_patterns(*_SCRATCH_IGNORE))
        originals: dict[str, str] = {}
        accepted: dict[int, dict[str, Any]] = {}  # id(site) -> sink, in application order
        routes = _RouteState(accepted=accepted)
        work = replace(config, root=scratch, write=True, test_cmd=None, routes=routes)
        slots: dict[int, int] = {}
        texts_of: dict[int, str] = {}
        covered, texts = _one_per_function(config.root, selected)
        base = sorted(selected, key=lambda item: not _names_removed_class(item))
        ordered = _producer_first(config.root, base, covered, routes.feeders)
        rank = {id(site): index for index, site in enumerate(base)}
        ranks: list[int] = []  # report order stays the usual one
        for site in ordered:
            if id(site) not in covered and not site.via_wrapper:
                routes.upcoming[id(site)] = (site, texts.get(id(site), ""))
        for site in ordered:
            routes.upcoming.pop(id(site), None)
            ranks.append(rank[id(site)])
            if id(site) in covered:
                report.results.append(SiteResult(site=site, status=SiteStatus.SKIPPED, why=covered[id(site)]))
                continue
            if site.via_wrapper:
                report.results.append(_apply_one(site, work, client, report))
                continue
            originals.setdefault(site.path, _read(config.root / site.path))
            texts_of[id(site)] = texts.get(id(site), "")
            moved = _relocate(scratch, site, texts_of[id(site)])
            sink: dict[str, Any] = {"site": site}
            routes.key = id(site)
            result = _apply_one(moved, work, client, report, sink=sink) if moved else None
            slots[id(site)] = len(report.results)
            report.results.append(result or SiteResult(site=site, status=SiteStatus.SKIPPED, why="line rewritten by an earlier fix in this file"))
            if result is not None and result.status == SiteStatus.FIXED_UNVERIFIED:
                accepted[id(site)] = sink
        _retry_routed(work, client, report, originals, texts_of, slots)
        _drop_traced_reads(work, report, originals, texts_of, slots)
        _final_recheck(config, scratch, report, originals, texts_of, slots, accepted, work, client)
        failure = ""
        for _round in range(config.settings.max_attempts + 1):
            ok, output = _run_cmd(config.test_cmd or "", scratch)
            if ok or not accepted:
                failure = "" if ok else output
                break
            failure = output
            blamed = _blame(output, scratch, accepted) or list(accepted)
            if _round == config.settings.max_attempts:
                break
            for key in blamed:
                sink = accepted.pop(key)
                site = sink["site"]
                _rebuild(scratch, originals, accepted, texts_of, site.path)
                moved = _relocate(scratch, site, texts_of[key])
                note = trim_test_feedback(output)
                retry_sink: dict[str, Any] = {"site": site}
                result = None
                used = report.results[slots[key]].attempts
                routes.key = key
                if moved is not None and used < config.settings.max_attempts and sink.get("tokens", 0) < config.settings.max_tokens_per_site:
                    feedback = (
                        "Your previous rewrite passed the static check but the tests failed:\n"
                        f"{sink.get('block', '')}\nTest output:\n{note}"
                    )
                    result = _apply_one(moved, work, client, report, feedback=feedback, spent=sink.get("tokens", 0), used=used, sink=retry_sink)
                if result is not None and result.status == SiteStatus.FIXED_UNVERIFIED:
                    accepted[key] = retry_sink
                    report.results[slots[key]] = result
                else:
                    _rebuild(scratch, originals, accepted, texts_of, site.path)
                    report.results[slots[key]] = _reverted(result or report.results[slots[key]], site, note)
            _retry_routed(work, client, report, originals, texts_of, slots)
        _retry_routed(work, client, report, originals, texts_of, slots)
        _drop_traced_reads(work, report, originals, texts_of, slots)
        if _final_recheck(config, scratch, report, originals, texts_of, slots, accepted, work, client) and accepted and not failure:
            ok, output = _run_cmd(config.test_cmd or "", scratch)
            failure = "" if ok else output
        if failure and accepted:
            # Revert what the last failure points at, then confirm the rest pass.
            for key in _blame(failure, scratch, accepted) or list(accepted):
                sink = accepted.pop(key)
                _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(failure))
            ok, output = _run_cmd(config.test_cmd or "", scratch) if accepted else (True, "")
            if ok and accepted and _final_recheck(config, scratch, report, originals, texts_of, slots, accepted, work, client) and accepted:
                # A rewrite that leaned on a reverted one was dropped too: confirm the rest again.
                ok, output = _run_cmd(config.test_cmd or "", scratch)
            if not ok:
                for key in list(accepted):
                    sink = accepted.pop(key)
                    _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                    report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(output))
        for key in accepted:
            done = report.results[slots[key]]
            why = _why_when_uncalled(accepted, key) or done.why
            report.results[slots[key]] = replace(done, status=SiteStatus.FIXED_VERIFIED, why=why)
        for path, before in originals.items():
            after = _read(scratch / path)
            for key in accepted:
                if report.results[slots[key]].site.path == path:
                    report.results[slots[key]].diff = unified_diff_for(path, before, after)
            if config.write and after != before:
                _write_file(config.root, path, after)
        if len(ranks) == len(report.results):
            report.results[:] = [item for _rank, item in sorted(zip(ranks, report.results), key=lambda pair: pair[0])]


_SCRATCH_IGNORE = (".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_cmd(command: str, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _rebuild(scratch: Path, originals: dict[str, str], accepted: dict[int, dict[str, Any]], texts: dict[int, str], path: str) -> None:
    """The file as its original plus every accepted edit, re-spliced in order."""
    _write_file(scratch, path, originals[path])
    for key, sink in list(accepted.items()):
        site = sink["site"]
        if site.path != path:
            continue
        moved = _relocate(scratch, site, texts.get(key, ""))
        try:
            if moved is None:
                raise PatchError("site line not found")
            patched, _diff, _span = splice_edit_with_span(build_context_pack(scratch, moved), sink["edit"])
        except (PatchError, ContextError):
            accepted.pop(key)
            continue
        _write_file(scratch, path, patched)


def _blame(output: str, scratch: Path, accepted: dict[int, dict[str, Any]]) -> list[int]:
    """Sites a test failure points at: a traceback line inside the rewrite, or a
    name the failure reports (ImportError, NameError, AttributeError) that the
    rewrite mentions. Otherwise the sites whose file the output names."""
    located: dict[str, list[int]] = {}
    for match in re.finditer(r'([\w./\\-]+\.py)(?::|", line )(\d+)', output):
        located.setdefault(match.group(1).replace("\\", "/"), []).append(int(match.group(2)))
    names = set(re.findall(r"(?:cannot import name|name|attribute) '(\w+)'", output))
    by_line: list[int] = []
    by_file: list[int] = []
    for key, sink in accepted.items():
        path = sink["site"].path
        block = sink.get("block", "")
        text = _read(scratch / path)
        at = text.find(block) if block else -1
        first = text.count("\n", 0, at) + 1 if at >= 0 else 0
        last = first + block.count("\n")
        lines = [n for shown, found in located.items() if shown.endswith(path) for n in found]
        if (at >= 0 and any(first <= n <= last for n in lines)) or any(re.search(rf"\b{name}\b", block) for name in names):
            by_line.append(key)
        elif lines or path in output:
            by_file.append(key)
    return by_line or by_file


def _reverted(result: SiteResult, site: ImpactSite, note: str) -> SiteResult:
    return SiteResult(
        site=site,
        status=SiteStatus.NEEDS_HUMAN,
        diff=result.diff,
        attempts=result.attempts,
        error="reverted: the tests still fail with this rewrite\n" + note,
        context=result.context,
    )


def _names_removed_class(site: ImpactSite) -> bool:
    tail = (site.expr.split() or [""])[-1].rsplit(".", 1)[-1]
    return site.kind == "removed" and tail[:1].isupper() and tail == site.symbol.rsplit(".", 1)[-1]


def _one_per_function(root: Path, sites: list[ImpactSite]) -> tuple[dict[int, str], dict[int, str]]:
    """The rewrite replaces a whole function and the recheck covers its span,
    so one site per enclosing function is enough; the rest point at it."""
    first: dict[tuple[str, int], ImpactSite] = {}
    covered: dict[int, str] = {}
    texts: dict[int, str] = {}
    for site in sites:
        if site.via_wrapper:
            continue
        try:
            lines = (root / site.path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        try:
            span = enclosing_function("\n".join(lines), site.line)
        except ContextError:
            span = None
        key = (site.path, span[2] if span else -site.line)
        lead = first.setdefault(key, site)
        if lead is not site:
            covered[id(site)] = f"same function as {lead.path}:{lead.line}; covered by that rewrite and its recheck"
        elif 0 < site.line <= len(lines):
            texts[id(site)] = lines[site.line - 1].strip()
    return covered, texts


def _relocate(root: Path, site: ImpactSite, text: str) -> ImpactSite | None:
    """Earlier written fixes shift lines: find the site's original line text again."""
    try:
        lines = (root / site.path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return site
    if not text or (0 < site.line <= len(lines) and lines[site.line - 1].strip() == text):
        return site
    hits = [number for number, line in enumerate(lines, 1) if line.strip() == text]
    if not hits:
        return None
    return replace(site, line=min(hits, key=lambda number: abs(number - site.line)))


def _apply_one(
    site: ImpactSite,
    config: ApplyConfig,
    client: ModelClient | None,
    report: ApplyReport,
    *,
    feedback: str = "",
    spent: int = 0,
    used: int = 0,
    sink: dict[str, Any] | None = None,
) -> SiteResult:
    if site.via_wrapper:
        return SiteResult(
            site=site,
            status=SiteStatus.SKIPPED,
            why="wrapper caller; fixed at the SDK line",
        )
    try:
        pack = build_context_pack(config.root, site)
    except ContextError as exc:
        return SiteResult(site=site, status=SiteStatus.NEEDS_HUMAN, error=str(exc))

    if config.dry_run:
        return SiteResult(
            site=site,
            status=SiteStatus.DRY_RUN,
            why="context pack only (--dry-run); model not called",
            context=pack,
        )

    if client is None:
        return SiteResult(
            site=site,
            status=SiteStatus.NEEDS_HUMAN,
            error="no model client available",
            context=pack,
        )

    target = _new_signatures(config, site, [site.symbol])
    if target:
        pack.release_diff["new_release_signatures"] = target
    pack.connected = _connected(config, site, pack)

    last_error = ""
    last_diff = ""
    earlier: list[str] = []
    attempts = used
    site_tokens = spent
    sink = sink if sink is not None else {}
    for attempt in range(used + 1, config.settings.max_attempts + 1):
        attempts = attempt
        remaining = config.settings.max_tokens_per_site - site_tokens
        if remaining <= 0:
            last_error = "token budget exhausted for this site"
            break
        try:
            edit_text, used = _propose(
                client,
                pack,
                config.settings,
                feedback=feedback,
                max_tokens=min(2048, remaining),
            )
            site_tokens += used
            report.tokens_used += used
            edit = parse_model_edit(edit_text)
            patched, diff, span = splice_edit_with_span(pack, edit)
            last_diff = diff
            repeated = _repeated_decorator(patched, span)
            if repeated:
                raise PatchError(
                    f"rewrite repeats a decorator the function already has ({repeated}); "
                    "give function_source without the existing decorators or list each once"
                )
            flows = _verify_site_cleared(config, site, patched, span)
            tests_ok, test_err = _run_tests(config, site, patched)
            if not tests_ok:
                feedback = trim_test_feedback(test_err or "tests failed")
                last_error = feedback
                continue
            why = ""
            why_budget = config.settings.max_tokens_per_site - site_tokens
            if why_budget > 0:
                why, why_tokens = _summarize(
                    client, pack, diff, config.settings, max_tokens=min(128, why_budget)
                )
                site_tokens += why_tokens
                report.tokens_used += why_tokens
            status = (
                SiteStatus.FIXED_VERIFIED
                if config.test_cmd
                else SiteStatus.FIXED_UNVERIFIED
            )
            if config.write:
                _write_file(config.root, site.path, patched)
            sink.update(edit=edit, block="".join(patched.splitlines(keepends=True)[span[0] - 1 : span[1]]))
            sink["tokens"] = site_tokens
            sink["flows"] = flows
            return SiteResult(
                site=site,
                status=status,
                why=why,
                diff=diff,
                attempts=attempts,
                context=pack,
            )
        except RecheckError as exc:
            last_error = str(exc)
            feedback = _with_earlier(last_error, exc.findings, earlier)
            earlier.extend(text for text in exc.findings if text not in earlier)
        except PatchError as exc:
            last_error = str(exc)
            feedback = last_error
        except Exception as exc:  # noqa: BLE001 — surface to retry / needs-human
            last_error = str(exc)
            feedback = last_error

    sink["tokens"] = site_tokens
    return SiteResult(
        site=site,
        status=SiteStatus.NEEDS_HUMAN,
        why="",
        diff=last_diff,
        attempts=attempts,
        error=last_error.split("\nNew release signatures", 1)[0] or "max attempts exhausted",
        context=pack,
    )


def _propose(
    client: ModelClient,
    pack: ContextPack,
    settings: TokenFactorySettings,
    *,
    feedback: str,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in propose_messages(pack, feedback=feedback)
    ]
    result = client.chat(
        model=settings.model,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=settings.enable_thinking,
    )
    return result.content, result.total_tokens


def _summarize(
    client: ModelClient,
    pack: ContextPack,
    diff: str,
    settings: TokenFactorySettings,
    *,
    max_tokens: int,
) -> tuple[str, int]:
    messages = [
        ChatMessage(role=role, content=content)
        for role, content in summarize_messages(pack, diff)
    ]
    result = client.chat(
        model=settings.model_small,
        messages=messages,
        max_tokens=max_tokens,
        enable_thinking=settings.enable_thinking_small,
    )
    text = result.content.strip()
    why = text.splitlines()[0] if text else ""
    return why, result.total_tokens


def _verify_site_cleared(
    config: ApplyConfig, site: ImpactSite, patched: str, span: tuple[int, int]
) -> list[dict[str, Any]]:
    """Raise when the rewrite still breaks; return the release values it passes on."""
    flows: list[dict[str, Any]] = []
    if config.site_cleared is None and config.recheck_impact and config.routes is not None:
        token = FLOWS.set(flows)
        try:
            _verify_against_release(config, site, patched, span)
        finally:
            FLOWS.reset(token)
        return flows
    _verify_against_release(config, site, patched, span)
    return flows


def _verify_against_release(config: ApplyConfig, site: ImpactSite, patched: str, span: tuple[int, int]) -> None:
    if config.site_cleared is not None:
        if not config.site_cleared(config.root, site, patched):
            raise PatchError("impact site still flagged after patch")
        return
    if not config.recheck_impact:
        return
    breaks = remaining_breaks(
        config.root,
        site,
        patched,
        span=span,
        fixtures=config.fixtures,
        allow_network=config.allow_network,
    )
    breaks = _route_breaks(config, site, patched, span, breaks)
    if breaks:
        # The finding is on the first line: a report cell keeps only that line.
        message = _breaks_message([text for text, _symbol in breaks])
        signatures = _new_signatures(config, site, [symbol for _text, symbol in breaks])
        if signatures:
            message += "\nNew release signatures (read from its own code):\n" + "\n".join(
                signatures.values()
            )
        raise RecheckError(message, [text for text, _symbol in breaks])


def _breaks_message(texts: list[str]) -> str:
    """Reason text whose first line contains the finding, not only the prefix."""
    shown: list[str] = []
    for text in texts:
        text = (text or "").strip()
        if text and text not in shown:
            shown.append(text)
    if not shown:
        return "rewrite still breaks against the new release"
    return _fields_once("rewrite still breaks against the new release: " + "; ".join(shown))


_FIELDS_AT = re.compile(r"\((?:([\w, ]+) )?fields: ")


def _field_groups(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, type names)`` of each ``(Name fields: ...)`` list in ``text``."""
    out: list[tuple[int, int, str]] = []
    at = 0
    while True:
        match = _FIELDS_AT.search(text, at)
        if match is None:
            return out
        depth, end = 0, match.start()
        while end < len(text):
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
            if depth == 0:
                break
        out.append((match.start(), end, match.group(1) or ""))
        at = end


def _fields_once(text: str, earlier: dict[str, str] | None = None) -> str:
    """Keep each field list the first time it appears.

    A copy later in the same text says ``(Name fields as above)``; one listed in
    an earlier result (``earlier`` maps the list to that site) says where."""
    seen: set[str] = set()
    out: list[str] = []
    at = 0
    for start, end, names in _field_groups(text):
        group = text[start:end]
        out.append(text[at:start])
        who = f"{names} fields" if names else "fields"
        if earlier and group in earlier:
            out.append(f"({who} listed at {earlier[group]})")
        elif group in seen:
            out.append(f"({who} as above)")
        else:
            out.append(group)
        seen.add(group)
        at = end
    out.append(text[at:])
    return "".join(out)


def _fill_why(report: ApplyReport) -> None:
    """No fixed or needs-human result goes out without a reason, and a type's
    field list is printed once across the whole report."""
    listed: dict[str, str] = {}
    for index, result in enumerate(report.results):
        why = result.why
        if not why.strip() and result.status in {
            SiteStatus.FIXED_VERIFIED, SiteStatus.FIXED_UNVERIFIED, SiteStatus.NEEDS_HUMAN
        }:
            if result.status == SiteStatus.NEEDS_HUMAN and result.error.strip():
                why = result.error.strip()
            else:
                why = _change_summary(result)
        shown_why, shown_error = _fields_once(why, listed), _fields_once(result.error, listed)
        label = f"{Path(result.site.path).name}:{result.site.line}"
        for text in (why, result.error):
            for start, end, _names in _field_groups(text):
                listed.setdefault(text[start:end], label)
        if (shown_why, shown_error) != (result.why, result.error):
            report.results[index] = replace(result, why=shown_why, error=shown_error)


def _change_summary(result: SiteResult) -> str:
    """A short reason built from the impact record when the model gave none."""
    site = result.site
    what = f"`{site.expr}`" if site.expr else f"{site.path}:{site.line}"
    because = f" ({site.change})" if site.change else ""
    if result.status == SiteStatus.NEEDS_HUMAN:
        return f"needs a human: {what}{because} was not fixed"
    return f"rewrote {what} for the new release{because}"


def _route_breaks(
    config: ApplyConfig,
    site: ImpactSite,
    patched: str,
    span: tuple[int, int],
    breaks: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Hand a read that lives in another accepted rewrite back to that rewrite.

    A finding inside this rewrite stays here. A finding in a helper this batch
    did not rewrite stays here too, so the caller still has to stop passing the
    value on. Only a read inside some other site's accepted rewrite moves.
    """
    routes = config.routes
    if routes is None:
        return breaks
    kept: list[tuple[str, str]] = []
    for text, symbol in breaks:
        owner = _finding_owner(config.root, routes, site, patched, span, text)
        if owner is None:
            later = _upcoming_owner(config.root, routes, site, span, text)
            if later is not None:
                # That site's rewrite gets it in its prompt, and is not verified
                # while the read is still in it.
                for bucket in (routes.deferred, routes.traced):
                    found = bucket.setdefault(later, [])
                    if text not in found:
                        found.append(text)
                continue
            kept.append((text, symbol))
            continue
        for bucket in (routes.pending, routes.traced):
            found = bucket.setdefault(owner, [])
            if text not in found:
                found.append(text)
    return kept


def _finding_owner(
    root: Path,
    routes: _RouteState,
    site: ImpactSite,
    patched: str,
    span: tuple[int, int],
    text: str,
) -> int | None:
    located = _finding_at(text)
    if located is None:
        return None
    path, line = located
    if _path_match(path, site.path) and span[0] <= line <= span[1]:
        return None
    for key, sink in routes.accepted.items():
        if key == routes.key:
            continue
        site_path = sink["site"].path.replace("\\", "/")
        if not _path_match(path, site_path):
            continue
        body = patched if _path_match(path, site.path) else _read(root / sink["site"].path)
        bounds = _span_in(body, sink.get("block") or "")
        if bounds is not None and bounds[0] <= line <= bounds[1]:
            return key
    return None


def _finding_at(text: str) -> tuple[str, int] | None:
    match = re.match(r"([^:\s].*?):(\d+)\b", text)
    if match is None:
        return None
    return match.group(1).replace("\\", "/"), int(match.group(2))


def _path_match(left: str, right: str) -> bool:
    a = left.replace("\\", "/").lstrip("./")
    b = right.replace("\\", "/").lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _span_in(text: str, block: str) -> tuple[int, int] | None:
    if not block:
        return None
    at = text.find(block)
    if at < 0:
        return None
    first = text.count("\n", 0, at) + 1
    last = first + block.count("\n") - (1 if block.endswith("\n") else 0)
    return first, max(last, first)


def _expr_remains(block: str, finding: str) -> bool:
    """True when ``finding`` names an attribute read that ``block`` still contains."""
    if "no attribute " not in finding:
        return False
    match = re.match(r"^[^:\n]+:\d+\s+(.*?):\s", finding)
    expr = match.group(1).strip() if match else ""
    if not expr:
        return False
    # Quote style is not part of the read; the model may swap ' and ".
    return expr.replace('"', "'") in block.replace('"', "'")


def _retry_routed(
    config: ApplyConfig,
    client: ModelClient,
    report: ApplyReport,
    originals: dict[str, str],
    texts: dict[int, str],
    slots: dict[int, int],
) -> None:
    """Retry each rewrite a routed finding points at, with that finding as feedback."""
    routes = config.routes
    if routes is None:
        return
    accepted = routes.accepted
    guard = 0
    limit = max(4, len(slots) * (config.settings.max_attempts + 1))
    while routes.pending and guard < limit:
        guard += 1
        key, findings = routes.pending.popitem()
        sink = accepted.get(key)
        if sink is None:
            continue
        accepted.pop(key)
        site = sink["site"]
        _rebuild(config.root, originals, accepted, texts, site.path)
        moved = _relocate(config.root, site, texts.get(key, ""))
        note = _breaks_message(findings)
        block = sink.get("block") or ""
        if block:
            note += "\nThe rewrite this refers to:\n" + block
        routes.key = key
        retry_sink: dict[str, Any] = {"site": site}
        result = None
        used = report.results[slots[key]].attempts
        if (
            moved is not None
            and used < config.settings.max_attempts
            and sink.get("tokens", 0) < config.settings.max_tokens_per_site
        ):
            result = _apply_one(
                moved,
                config,
                client,
                report,
                feedback=note,
                spent=sink.get("tokens", 0),
                used=used,
                sink=retry_sink,
            )
        if result is not None and result.status == SiteStatus.FIXED_UNVERIFIED:
            accepted[key] = retry_sink
            report.results[slots[key]] = result
            continue
        _rebuild(config.root, originals, accepted, texts, site.path)
        previous = result or report.results[slots[key]]
        error = previous.error if previous.error else note
        report.results[slots[key]] = SiteResult(
            site=site,
            status=SiteStatus.NEEDS_HUMAN,
            diff=previous.diff,
            attempts=previous.attempts,
            error=error,
            context=previous.context,
        )


_UNCALLED_WHY = "no rewritten site calls this function"


def _why_when_uncalled(accepted: dict[int, dict[str, Any]], key: int) -> str:
    """Why text when a loosened ``Any`` parameter is still attribute-read and no
    other rewritten site calls the function. Otherwise empty, so the model's
    sentence stands."""
    name = _any_param_still_read(accepted.get(key, {}).get("block") or "")
    if not name:
        return ""
    for other, sink in accepted.items():
        if other != key and _calls(sink.get("block") or "", name):
            return ""
    return _UNCALLED_WHY


def _any_param_still_read(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if _annotation_is_any(arg.annotation)
        }
        if not params:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in params:
                return node.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in {"getattr", "hasattr"}
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id in params
            ):
                return node.name
    return ""


def _annotation_is_any(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip() in {"Any", "typing.Any"}
    if isinstance(node, ast.Name):
        return node.id == "Any"
    if isinstance(node, ast.Attribute):
        return node.attr == "Any"
    return False


def _calls(source: str, name: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called == name:
            return True
    return False


def _drop_traced_reads(
    config: ApplyConfig,
    report: ApplyReport,
    originals: dict[str, str],
    texts: dict[int, str],
    slots: dict[int, int],
) -> None:
    """A site is not verified while a masked-read finding still matches its rewrite."""
    routes = config.routes
    if routes is None:
        return
    for key, findings in list(routes.traced.items()):
        sink = routes.accepted.get(key)
        if sink is None:
            continue
        lingering = [text for text in findings if _expr_remains(sink.get("block") or "", text)]
        if not lingering:
            continue
        routes.accepted.pop(key)
        _rebuild(config.root, originals, routes.accepted, texts, sink["site"].path)
        previous = report.results[slots[key]]
        report.results[slots[key]] = SiteResult(
            site=sink["site"],
            status=SiteStatus.NEEDS_HUMAN,
            diff=previous.diff,
            attempts=previous.attempts,
            error=_breaks_message(lingering),
            context=previous.context,
        )


def _final_recheck(
    config: ApplyConfig,
    scratch: Path,
    report: ApplyReport,
    originals: dict[str, str],
    texts: dict[int, str],
    slots: dict[int, int],
    accepted: dict[int, dict[str, Any]],
    work: ApplyConfig | None = None,
    client: ModelClient | None = None,
) -> bool:
    """Recheck every accepted rewrite against the finished migration.

    Each site was checked when it was written, before later sites changed what
    flows into it (a caller's new return type reaching an ``Any`` parameter).
    A flagged site gets the finding back once (with ``work`` and ``client``),
    within its token budget. If that does not clear it, the site needs a human,
    with the finding as its reason; passing tests do not clear a finding.
    True when the tree changed (a site was retried or dropped)."""
    dropped = False
    if config.site_cleared is not None or not config.recheck_impact:
        return dropped
    # Dropping one rewrite restores its original code, which can break a rewrite
    # that calls it and was already rechecked: repeat until nothing more drops.
    # A finding inside another accepted rewrite is that rewrite's to answer.
    handed: dict[int, list[str]] = {}
    changed = True
    while changed and accepted:
        changed = False
        for key in list(accepted):
            if key not in accepted:
                continue
            sink = accepted[key]
            site = sink["site"]
            feeder = _unfixed_feeder(work, report, slots, accepted, key)
            if feeder:
                # What this function receives is still the old release's value.
                accepted.pop(key)
                previous = report.results[slots[key]]
                message = f"its caller {feeder} was not fixed (needs a human), so the value this rewrite expects never arrives"
                report.results[slots[key]] = SiteResult(
                    site=site, status=SiteStatus.NEEDS_HUMAN, diff=previous.diff, attempts=previous.attempts,
                    error=message, why=message, context=previous.context,
                )
                _rebuild(scratch, originals, accepted, texts, site.path)
                dropped = changed = True
                continue
            body = _read(scratch / site.path)
            span = _span_in(body, sink.get("block") or "")
            if span is None:
                continue
            try:
                breaks = remaining_breaks(
                    scratch, site, body, span=span, fixtures=config.fixtures,
                    allow_network=config.allow_network, original=originals.get(site.path, ""),
                )
            except Exception:  # noqa: BLE001 — a recheck that cannot run keeps the earlier verdict
                breaks = []
            findings: list[str] = []
            for text, _symbol in breaks:
                owner = _accepted_owner(scratch, accepted, key, span, site.path, text)
                if owner is None:
                    findings.append(text)
                elif text not in handed.setdefault(owner, []):
                    handed[owner].append(text)
                    changed = True
            findings += [text for text in handed.pop(key, []) if text not in findings]
            if not findings:
                continue
            accepted.pop(key)
            previous = report.results[slots[key]]
            message = _breaks_message(findings)
            if _retry_final(work, client, report, originals, texts, slots, key, sink, message):
                dropped = changed = True
                continue
            depends = _unresolved_dependencies(scratch, report, texts, slots, accepted, findings)
            if depends:
                message += "; depends on " + ", ".join(depends) + ", whose fix was not applied (needs a human)"
            report.results[slots[key]] = SiteResult(
                site=site,
                status=SiteStatus.NEEDS_HUMAN,
                diff=previous.diff,
                attempts=previous.attempts,
                error=message,
                why=message,
                context=previous.context,
            )
            _rebuild(scratch, originals, accepted, texts, site.path)
            dropped = changed = True
    return dropped


def _unfixed_feeder(
    work: ApplyConfig | None, report: ApplyReport, slots: dict[int, int], accepted: dict[int, dict[str, Any]], key: int
) -> str:
    """``path:line`` of a site that calls this site's function and ended needs-human."""
    routes = work.routes if work is not None else None
    if routes is None:
        return ""
    for other in routes.feeders.get(key, []):
        if other in accepted or other not in slots:
            continue
        result = report.results[slots[other]]
        if result.status == SiteStatus.NEEDS_HUMAN:
            return f"{result.site.path}:{result.site.line}"
    return ""


def _repeated_decorator(patched: str, span: tuple[int, int]) -> str:
    """A decorator listed twice on a rewritten def (the splice kept the old one too)."""
    try:
        tree = ast.parse(patched)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and span[0] <= node.lineno <= span[1]:
            seen: list[str] = []
            for deco in node.decorator_list:
                text = ast.unparse(deco)
                if text in seen:
                    return f"@{text} on {node.name}"
                seen.append(text)
    return ""


def _accepted_owner(
    scratch: Path, accepted: dict[int, dict[str, Any]], key: int, span: tuple[int, int], path: str, text: str
) -> int | None:
    """The other accepted rewrite whose block holds this finding, if any."""
    located = _finding_at(text)
    if located is None or (_path_match(located[0], path) and span[0] <= located[1] <= span[1]):
        return None
    for other, sink in accepted.items():
        if other == key or not _path_match(located[0], sink["site"].path):
            continue
        bounds = _span_in(_read(scratch / sink["site"].path), sink.get("block") or "")
        if bounds is not None and bounds[0] <= located[1] <= bounds[1]:
            return other
    return None


def _retry_final(
    work: ApplyConfig | None,
    client: ModelClient | None,
    report: ApplyReport,
    originals: dict[str, str],
    texts: dict[int, str],
    slots: dict[int, int],
    key: int,
    sink: dict[str, Any],
    message: str,
) -> bool:
    """One more attempt for a site the final recheck flagged; True when it is accepted again.

    The site is already out of ``accepted``. On failure its file is rebuilt
    without it, as a plain drop would leave it."""
    routes = work.routes if work is not None else None
    site = sink["site"]
    if routes is None or client is None or key in routes.retried:
        return False
    routes.retried.add(key)
    accepted = routes.accepted
    _rebuild(work.root, originals, accepted, texts, site.path)
    moved = _relocate(work.root, site, texts.get(key, ""))
    if moved is None or sink.get("tokens", 0) >= work.settings.max_tokens_per_site:
        return False
    feedback = message
    if sink.get("block"):
        feedback += "\nThe rewrite this refers to:\n" + sink["block"]
    routes.key = key
    retry_sink: dict[str, Any] = {"site": site}
    result = _apply_one(
        moved, work, client, report, feedback=feedback, spent=sink.get("tokens", 0),
        used=max(0, work.settings.max_attempts - 1), sink=retry_sink,
    )
    if result.status != SiteStatus.FIXED_UNVERIFIED:
        _rebuild(work.root, originals, accepted, texts, site.path)
        return False
    accepted[key] = retry_sink
    report.results[slots[key]] = result
    return True


def _producer_first(
    root: Path, sites: list[ImpactSite], covered: dict[int, str], feeders: dict[int, list[int]] | None = None
) -> list[ImpactSite]:
    """Order so a site whose function calls another site's function is rewritten first.

    The producer's rewrite decides what value the consumer receives; the
    consumer is then rewritten with that value's new-release type in view.
    Otherwise the given order stands."""
    info: dict[int, tuple[str, set[str]]] = {}
    for site in sites:
        if id(site) in covered or site.via_wrapper:
            continue
        source = _read(root / site.path)
        try:
            found = enclosing_function(source, site.line)
            tree = ast.parse(source)
        except (ContextError, SyntaxError):
            continue
        if not found:
            continue
        name, _text, start, end = found
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and start <= node.lineno <= end
        }
        info[id(site)] = (name, calls - {name, ""})
    producers: dict[int, list[ImpactSite]] = {}
    for consumer in sites:
        if id(consumer) not in info:
            continue
        name = info[id(consumer)][0]
        producers[id(consumer)] = [
            other for other in sites
            if other is not consumer and id(other) in info and name in info[id(other)][1]
        ]
    if feeders is not None:
        feeders.update({key: [id(other) for other in found] for key, found in producers.items() if found})
    out: list[ImpactSite] = []
    done: set[int] = set()

    def visit(site: ImpactSite, stack: set[int]) -> None:
        if id(site) in done or id(site) in stack:
            return
        stack.add(id(site))
        for producer in producers.get(id(site), []):
            visit(producer, stack)
        stack.discard(id(site))
        done.add(id(site))
        out.append(site)

    for site in sites:
        visit(site, set())
    return out


def _upcoming_owner(root: Path, routes: _RouteState, site: ImpactSite, span: tuple[int, int], text: str) -> int | None:
    """The not-yet-rewritten site whose function holds this finding, if any."""
    located = _finding_at(text)
    if located is None:
        return None
    path, line = located
    if _path_match(path, site.path) and span[0] <= line <= span[1]:
        return None
    for key, (other, line_text) in routes.upcoming.items():
        if not _path_match(path, other.path):
            continue
        moved = _relocate(root, other, line_text) or other
        try:
            found = enclosing_function(_read(root / moved.path), moved.line)
        except ContextError:
            found = None
        first, last = (found[2], found[3]) if found else (moved.line, moved.line)
        if first <= line <= last:
            return key
    return None


def _connected(config: ApplyConfig, site: ImpactSite, pack: ContextPack) -> list[dict[str, Any]]:
    """What already rewritten sites now pass into this function, typed by the new release."""
    routes = config.routes
    if routes is None or not pack.function_name:
        return []
    out: list[dict[str, Any]] = []
    for key, sink in routes.accepted.items():
        if key == routes.key:
            continue
        for flow in sink.get("flows") or []:
            if flow.get("callee") != pack.function_name or not _path_match(flow.get("callee_path", ""), site.path):
                continue
            item = {
                "from": flow["from"],
                "passes": f"{flow['param']} = {flow['types']} ({flow['release']})",
                "fields": flow.get("fields") or [],
                "rewritten_code": sink.get("block") or "",
            }
            if item not in out:
                out.append(item)
    findings = routes.deferred.get(routes.key) or []
    if findings:
        out.append({"findings_in_this_function": list(findings)})
    return out


def _unresolved_dependencies(
    scratch: Path,
    report: ApplyReport,
    texts: dict[int, str],
    slots: dict[int, int],
    accepted: dict[int, dict[str, Any]],
    findings: list[str],
) -> list[str]:
    """Sites left for a human whose function holds a finding: the rewrite leans on them."""
    out: list[str] = []
    for key, index in slots.items():
        result = report.results[index]
        if key in accepted or result.status != SiteStatus.NEEDS_HUMAN:
            continue
        moved = _relocate(scratch, result.site, texts.get(key, "")) or result.site
        body = _read(scratch / moved.path)
        try:
            span = enclosing_function(body, moved.line)
        except ContextError:
            span = None
        first, last = (span[2], span[3]) if span else (moved.line, moved.line)
        for text in findings:
            located = _finding_at(text)
            if located and _path_match(located[0], moved.path) and first <= located[1] <= last:
                label = f"{result.site.path}:{result.site.line}"
                if label not in out:
                    out.append(label)
                break
    return out


class RecheckError(PatchError):
    """Recheck findings for one attempt; ``findings`` carries them one by one."""

    def __init__(self, message: str, findings: list[str]) -> None:
        super().__init__(message)
        self.findings = findings


def _with_earlier(message: str, current: list[str], earlier: list[str]) -> str:
    """Remind the model of findings an earlier attempt hit that this one did not repeat."""
    missing = [text for text in earlier if text not in current]
    if not missing:
        return message
    head, sep, tail = message.partition("\nNew release signatures")
    head += "\nEarlier attempts also broke (keep these fixed):\n- " + "\n- ".join(missing)
    return head + sep + tail


def _new_signatures(config: ApplyConfig, site: ImpactSite, symbols: list[str]) -> dict[str, str]:
    """Signatures of ``symbols`` in the new release's own code, when it defines them."""
    if not config.recheck_impact or not any(symbols):
        return {}
    try:
        wheel = new_release_wheel(
            site.dependency,
            site.to_version,
            fixtures=config.fixtures,
            allow_network=config.allow_network,
        )
        return signatures_for(wheel, [symbol for symbol in symbols if symbol])
    except Exception:  # noqa: BLE001 — prompt context only; never blocks a fix
        return {}


def _run_tests(
    config: ApplyConfig, site: ImpactSite, patched: str
) -> tuple[bool, str]:
    if not config.test_cmd:
        return True, ""
    with tempfile.TemporaryDirectory(prefix="conduit-apply-") as tmp:
        scratch = Path(tmp) / "tree"
        shutil.copytree(
            config.root,
            scratch,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
            ),
        )
        target = scratch / site.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched, encoding="utf-8")
        proc = subprocess.run(
            config.test_cmd,
            shell=True,
            cwd=scratch,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True, ""
        err = (proc.stdout or "") + (proc.stderr or "")
        return False, err.strip() or f"test command exited {proc.returncode}"


def _write_file(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
