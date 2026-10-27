"""Propose → splice → verify loop for each impact site."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from conduit.apply.client import ChatMessage, ModelClient
from conduit.apply.config import TokenFactorySettings
from conduit.apply.context import (
    ContextError,
    build_context_pack,
    enclosing_function,
    module_statement,
    name_uses,
    removed_names,
)
from conduit.apply.feedback import trim_test_feedback
from conduit.apply.nonecheck import NONE_CHECK
from conduit.apply.models import ContextPack, ImpactSite, SiteResult, SiteStatus
from conduit.apply.plain import plain_why
from conduit.apply.prompts import propose_messages, summarize_messages
from conduit.apply.unused import drop_unused_imports
from conduit.apply.splice import (
    PatchError,
    parse_model_edit,
    resolve_line_edits,
    splice_edit_with_span,
    unified_diff_for,
)
from conduit.apply.signatures import _modules, literal_value, new_release_wheel, signatures_for
from conduit.apply.testrun import Baseline, describe_preexisting, focus, judge, run_tests
from conduit.apply.change_why import caught, change_why
from conduit.apply.dropped import DROPPED, import_swap_why
from conduit.apply.producers import repo_producers
from conduit.apply.typecheck import FLOWS, parameter_brief, structured_requirements
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
    #: key -> keys of the sites this one depends on (its callers, or the import
    #: or definition it uses), outside its own dependency cycle.
    feeders: dict[int, list[int]] = field(default_factory=dict)
    #: (consumer, producer) -> "call" or "import", for the reason text.
    edge_kind: dict[tuple[int, int], str] = field(default_factory=dict)
    #: key -> every site rewritten before it whose finished code it builds on.
    producers: dict[int, list[int]] = field(default_factory=dict)


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
    #: Per-test outcomes on the PR's base state; tests that failed there do not
    #: count against a rewrite (see ``conduit.apply.testrun``).
    baseline: Baseline | None = None


@dataclass
class ApplyReport:
    results: list[SiteResult] = field(default_factory=list)
    tokens_used: int = 0
    #: Set only when a baseline was given: failures of the last test run that
    #: also failed on the base, and those that did not.
    preexisting: list[str] | None = None
    new_failures: list[str] | None = None
    #: Model calls this run made: endpoint (host and path), model, thinking
    #: setting, call count and tokens, one entry per model and setting.
    model_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def preexisting_note(self) -> str:
        return describe_preexisting(self.preexisting or [])

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "results": [item.to_dict() for item in self.results],
            "tokens_used": self.tokens_used,
            "summary": {
                status.value: sum(1 for r in self.results if r.status == status)
                for status in SiteStatus
            },
        }
        if self.model_calls:
            data["model_calls"] = [dict(item) for item in self.model_calls]
        if self.preexisting is not None:
            data["preexisting_failures"] = list(self.preexisting)
            data["preexisting_note"] = self.preexisting_note
            data["new_failures"] = list(self.new_failures or [])
        return data


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
        summary = getattr(client, "call_summary", None)
        if callable(summary):
            report.model_calls = summary()
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
        ordered = _dependency_order(config.root, base, covered, routes)
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
            blocked = _unfixed_feeder(work, report, slots, accepted, id(site), upfront=True)
            if blocked:
                # Not rewritten against a base that is still broken.
                result = SiteResult(site=site, status=SiteStatus.NEEDS_HUMAN, error=blocked, why=blocked)
            else:
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
            ok, output = _check(config, scratch, report)
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
            ok, output = _check(config, scratch, report)
            failure = "" if ok else output
        if failure and accepted:
            # Revert what the last failure points at, then confirm the rest pass.
            for key in _blame(failure, scratch, accepted) or list(accepted):
                sink = accepted.pop(key)
                _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(failure))
            ok, output = _check(config, scratch, report) if accepted else (True, "")
            if ok and accepted and _final_recheck(config, scratch, report, originals, texts_of, slots, accepted, work, client) and accepted:
                # A rewrite that leaned on a reverted one was dropped too: confirm the rest again.
                ok, output = _check(config, scratch, report)
            if not ok:
                for key in list(accepted):
                    sink = accepted.pop(key)
                    _rebuild(scratch, originals, accepted, texts_of, sink["site"].path)
                    report.results[slots[key]] = _reverted(report.results[slots[key]], sink["site"], trim_test_feedback(output))
        for key in accepted:
            done = report.results[slots[key]]
            hold = accepted[key].get("hold")
            if hold:
                report.results[slots[key]] = replace(done, status=SiteStatus.NEEDS_HUMAN, error=hold, why=hold)
                continue
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


def _check(config: ApplyConfig, scratch: Path, report: ApplyReport) -> tuple[bool, str]:
    """Run the tests. Without a baseline, the exit code decides. With one, only
    failures that did not already happen on the base count; the text returned
    for blame and feedback then holds just those."""
    if config.baseline is None:
        return _run_cmd(config.test_cmd or "", scratch)
    run = run_tests(config.test_cmd or "", scratch)
    verdict = judge(run, config.baseline)
    report.preexisting = verdict.preexisting
    report.new_failures = verdict.new + verdict.missing
    return verdict.passed, ("" if verdict.passed else focus(run, verdict))


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
            pack = build_context_pack(scratch, moved)
            patched, _diff, span = splice_edit_with_span(pack, sink["edit"])
            patched, _span = drop_unused_imports(pack.full_source, patched, span, path, scratch)
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
    resolved: list[str] | None = None,
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
    requires = _required_parameters(config, site, [site.symbol])
    if not requires:
        # A removed callable: what each replacement candidate needs, so the first
        # rewrite builds the arguments it takes instead of keeping the old ones.
        requires = _required_parameters(config, site, [c.get("symbol", "") for c in pack.candidates])
    if requires:
        pack.release_diff["new_release_requires"] = requires
        helpers = _repo_helpers(
            config,
            site,
            (pack.function_start_line, pack.function_end_line or pack.function_start_line),
            [site.symbol, *[c.get("symbol", "") for c in pack.candidates]],
        )
        if helpers:
            pack.release_diff["existing_repo_helpers_that_produce_this_type"] = helpers
    pack.release_diff.update(_constant_values(config, site))
    pack.connected = _connected(config, site, pack)
    pack.removed_name_uses = _with_received_types(config, site, pack.removed_name_uses)
    pack.producer_rewrites = _producer_rewrites(config, pack)

    last_error = ""
    last_diff = ""
    earlier: list[str] = []
    mismatched: set[tuple[str, str, str]] = set()
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
            edit = resolve_line_edits(pack, parse_model_edit(edit_text))
            patched, diff, span = splice_edit_with_span(pack, edit)
            cleaned, span = drop_unused_imports(pack.full_source, patched, span, site.path, config.root)
            if cleaned != patched:
                patched, diff = cleaned, unified_diff_for(pack.file_path, pack.full_source, cleaned)
            last_diff = diff
            repeated = _repeated_decorator(patched, span)
            if repeated:
                raise PatchError(
                    f"rewrite repeats a decorator the function already has ({repeated}); "
                    "give function_source without the existing decorators or list each once"
                )
            drops: dict[str, Any] = {}
            flows = _verify_site_cleared(config, site, patched, span, drops)
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
            # An import line reads from the two sources, not from the model's sentence.
            swapped = import_swap_why(pack.full_source, patched, site.line, site.change)
            if swapped:
                why = swapped
            else:
                # A fix a type check shaped, or one the model gave no sentence for,
                # says what changed from the two sources and the findings it cleared.
                cleared = [*(resolved or []), *earlier]
                if caught(cleared) or not why.strip():
                    why = change_why(
                        pack.full_source, patched, site.line, span, site.expr, site.change, cleared
                    ) or why
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
                dropped=list(drops.get("names") or []),
                dropped_note=drops.get("note") or "",
            )
        except RecheckError as exc:
            last_error = str(exc)
            repeated = _type_mismatches(exc.findings) & mismatched
            if repeated:
                # A plain type mismatch was already explained once: a further
                # attempt rarely does better, so it is handed off for review.
                last_error = "argument type still incompatible after one retry: " + last_error
                break
            mismatched |= _type_mismatches(exc.findings)
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
        error=last_error.split("\nNew release signatures", 1)[0].split("\nWhat it requires", 1)[0]
        or "max attempts exhausted",
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
    turns = [
        ChatMessage(role=role, content=content)
        for role, content in propose_messages(pack, feedback=feedback)
    ]
    result = client.chat(
        model=settings.model,
        turns=turns,
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
    turns = [
        ChatMessage(role=role, content=content)
        for role, content in summarize_messages(pack, diff)
    ]
    result = client.chat(
        model=settings.model_small,
        turns=turns,
        max_tokens=max_tokens,
        enable_thinking=settings.enable_thinking_small,
    )
    text = result.content.strip()
    why = text.splitlines()[0] if text else ""
    return why, result.total_tokens


def _verify_site_cleared(
    config: ApplyConfig,
    site: ImpactSite,
    patched: str,
    span: tuple[int, int],
    dropped: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Raise when the rewrite still breaks; return the release values it passes on.

    ``dropped`` is filled with the arguments the rewrite stopped passing."""
    flows: list[dict[str, Any]] = []
    drop_token = DROPPED.set(dropped)
    try:
        if config.site_cleared is None and config.recheck_impact and config.routes is not None:
            token = FLOWS.set(flows)
            try:
                _verify_against_release(config, site, patched, span)
            finally:
                FLOWS.reset(token)
            return flows
        _verify_against_release(config, site, patched, span)
        return flows
    finally:
        DROPPED.reset(drop_token)


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
        requires = _required_parameters(config, site, [symbol for _text, symbol in breaks])
        if requires:
            message += "\nWhat it requires (read from its own code):\n" + "\n".join(requires)
            helpers = _repo_helpers(config, site, span, [symbol for _text, symbol in breaks])
            if helpers:
                message += "\nExisting repo helpers that produce this type:\n" + "\n".join(
                    f"- {item['helper']} ({item['import']}"
                    + (f"; call as {item['call']}" if item.get("call") else "")
                    + (f"; {item['note']}" if item.get("note") else "")
                    + ")"
                    + (f"\n  Caution: {item['caution']}" if item.get("caution") else "")
                    for item in helpers
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
    field list is printed once across the whole report.

    A needs-human ``why`` is one plain sentence for people; the full finding,
    with field lists and the instructions retry prompts carry, stays in ``error``."""
    listed: dict[str, str] = {}
    for index, result in enumerate(report.results):
        why = result.why
        if result.status == SiteStatus.NEEDS_HUMAN:
            why = plain_why(result.error or result.why, result.site.path) or _change_summary(result)
        elif not why.strip() and result.status in {SiteStatus.FIXED_VERIFIED, SiteStatus.FIXED_UNVERIFIED}:
            why = _change_summary(result)
        if result.dropped_note and result.status in {SiteStatus.FIXED_VERIFIED, SiteStatus.FIXED_UNVERIFIED}:
            # One line: a report cell keeps only the first line of ``why``.
            if result.dropped_note not in why:
                why = " ".join(why.split()) + " " + result.dropped_note
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
        for owner, texts_found in _undefined_names(scratch, originals, accepted, texts).items():
            for text in texts_found:
                if text not in handed.setdefault(owner, []):
                    handed[owner].append(text)
        for key in list(accepted):
            if key not in accepted:
                continue
            sink = accepted[key]
            site = sink["site"]
            if sink.get("hold"):
                continue
            message = _unfixed_feeder(work, report, slots, accepted, key)
            if message:
                # What this function receives or uses is still the old release's.
                accepted.pop(key)
                previous = report.results[slots[key]]
                report.results[slots[key]] = SiteResult(
                    site=site, status=SiteStatus.NEEDS_HUMAN, diff=previous.diff, attempts=previous.attempts,
                    error=message, why=message, context=previous.context,
                )
                _rebuild(scratch, originals, accepted, texts, site.path)
                dropped = changed = True
                continue
            body = _read(scratch / site.path)
            span = _span_in(body, sink.get("block") or "")
            breaks: list[tuple[str, str]] = []
            if span is None and not handed.get(key):
                continue
            try:
                if span is not None:
                    # The finished migration: Optional values that reach a place None breaks count too.
                    none_token = NONE_CHECK.set(True)
                    try:
                        breaks = remaining_breaks(
                            scratch, site, body, span=span, fixtures=config.fixtures,
                            allow_network=config.allow_network, original=originals.get(site.path, ""),
                        )
                    finally:
                        NONE_CHECK.reset(none_token)
            except Exception:  # noqa: BLE001 — a recheck that cannot run keeps the earlier verdict
                breaks = []
            span = span or (site.line, site.line)
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
            if _retry_final(work, client, report, originals, texts, slots, key, sink, message, findings):
                dropped = changed = True
                continue
            if all(_UNDEFINED in text for text in findings):
                # Reverting would bring back an import the new release no longer
                # has; the rewrite stays in the diff, but it is not verified.
                sink["hold"] = message
                accepted[key] = sink
                _rebuild(scratch, originals, accepted, texts, site.path)
                changed = True
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


_UNDEFINED = " is no longer imported or defined after the rewrite at "


def _undefined_names(
    scratch: Path, originals: dict[str, str], accepted: dict[int, dict[str, Any]], texts: dict[int, str]
) -> dict[int, list[str]]:
    """Names a changed file still uses (annotations included, even when they are
    never evaluated) that it no longer imports or defines, by the site whose
    original line bound them."""
    out: dict[int, list[str]] = {}
    by_path: dict[str, list[int]] = {}
    for key, sink in accepted.items():
        if not sink.get("hold"):
            by_path.setdefault(sink["site"].path, []).append(key)
    for path, keys in by_path.items():
        before, after = originals.get(path, ""), _read(scratch / path)
        if before == after:
            continue
        try:
            old_tree, new_tree = ast.parse(before), ast.parse(after)
        except SyntaxError:
            continue
        lost = _bound_names(old_tree) - _bound_names(new_tree)
        if not lost:
            continue
        uses = _used_names(new_tree)
        for name in sorted(lost):
            if name not in uses:
                continue
            owner = next((key for key in keys if name in _bound_by_line(texts.get(key, ""))), None)
            if owner is None:
                continue
            site = accepted[owner]["site"]
            line = uses[name]
            out.setdefault(owner, []).append(
                f"{path}:{line} {name}: name {name} is no longer imported or defined after the rewrite at "
                f"{path}:{site.line} removed it, but line {line} still uses it; it is not in the new release "
                f"under that import: keep the name defined, or use the type the call now returns, read from "
                f"the new release's code"
            )
    return out


def _bound_names(tree: ast.Module) -> set[str]:
    """Every name the module binds anywhere: imports, defs, classes, arguments, targets."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _used_names(tree: ast.Module) -> dict[str, int]:
    """Loaded names (first line), including those inside string annotations."""
    uses: dict[str, int] = {}

    def add(node: ast.AST, line: int) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                uses.setdefault(sub.id, line or getattr(sub, "lineno", 0))
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and line:
                try:
                    add(ast.parse(sub.value.strip(), mode="eval"), line)
                except SyntaxError:
                    pass

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            uses.setdefault(node.id, node.lineno)
        annotations: list[ast.AST] = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            add(annotation, annotation.lineno)
    return uses


def _bound_by_line(text: str) -> set[str]:
    """Names the site's original line binds (an import, def or assignment)."""
    try:
        return _bound_names(ast.parse(text.strip()))
    except SyntaxError:
        return set()


def _unfixed_feeder(
    work: ApplyConfig | None,
    report: ApplyReport,
    slots: dict[int, int],
    accepted: dict[int, dict[str, Any]],
    key: int,
    *,
    upfront: bool = False,
) -> str:
    """Reason text when a site this one depends on ended needs-human; else empty.

    ``upfront``: checked before the rewrite, which then does not happen."""
    routes = work.routes if work is not None else None
    if routes is None:
        return ""
    for other in routes.feeders.get(key, []):
        if other in accepted or other not in slots:
            continue
        result = report.results[slots[other]]
        if result.status != SiteStatus.NEEDS_HUMAN:
            continue
        where = f"{result.site.path}:{result.site.line}"
        if routes.edge_kind.get((key, other)) == "import":
            head = f"the import or definition at {where} that it uses was not fixed (needs a human)"
        else:
            head = f"its caller {where} was not fixed (needs a human)"
        if upfront:
            return head + ", so this site was not rewritten against it"
        if routes.edge_kind.get((key, other)) == "import":
            return head + ", so this rewrite builds on code that is not there"
        return head + ", so the value this rewrite expects never arrives"
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
    findings: list[str] | None = None,
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
        used=max(0, work.settings.max_attempts - 1), sink=retry_sink, resolved=findings,
    )
    if result.status != SiteStatus.FIXED_UNVERIFIED:
        _rebuild(work.root, originals, accepted, texts, site.path)
        return False
    accepted[key] = retry_sink
    report.results[slots[key]] = result
    return True


def _dependency_order(
    root: Path, sites: list[ImpactSite], covered: dict[int, str], routes: _RouteState | None = None
) -> list[ImpactSite]:
    """Rewrite order from a dependency graph across the sites: producers first.

    A site depends on another when it calls the other's function (the caller
    decides what value arrives), and when its code uses a name another site's
    module-level line binds (the import or definition it uses). An import of a
    removed name also waits for the callers of a function that has the name as
    a parameter annotation, so the type that now arrives there is known; that
    edge only orders, it does not block. A cycle is one group, rewritten in the
    given order. ``routes`` gets each site's blocking producers (outside its
    group), the kind of each edge, and every producer whose code it builds on."""
    info: dict[int, dict[str, Any]] = {}
    for site in sites:
        if id(site) in covered or site.via_wrapper:
            continue
        source = _read(root / site.path)
        try:
            tree = ast.parse(source)
            found = enclosing_function(source, site.line)
        except (ContextError, SyntaxError):
            continue
        binds: set[str] = set()
        if found is None:
            found = module_statement(source, site.line)
            if found is None:
                continue
            try:
                binds = _bound_names(ast.parse(textwrap.dedent(found[1])))
            except SyntaxError:
                binds = set()
        name, _text, start, end = found
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and start <= node.lineno <= end
        }
        loads = {used for used, line in _used_in_span(tree, (start, end))}
        annotated = {
            use["function"]
            for use in name_uses(source, removed_names(source, site), (start, end))
            if use.get("param") and not use.get("in_this_function")
        } if binds else set()
        info[id(site)] = {
            "site": site, "name": name, "calls": calls - {name, ""}, "loads": loads - binds,
            "binds": binds, "annotated": annotated,
        }
    nodes = [site for site in sites if id(site) in info]
    hard: dict[int, dict[int, str]] = {id(site): {} for site in nodes}
    soft: dict[int, set[int]] = {id(site): set() for site in nodes}
    for consumer in nodes:
        mine = info[id(consumer)]
        for producer in nodes:
            if producer is consumer:
                continue
            theirs = info[id(producer)]
            if mine["name"] and mine["name"] in theirs["calls"]:
                hard[id(consumer)].setdefault(id(producer), "call")
            elif theirs["binds"] and producer.path == consumer.path and theirs["binds"] & mine["loads"]:
                hard[id(consumer)].setdefault(id(producer), "import")
            if mine["annotated"] and mine["annotated"] & theirs["calls"]:
                soft[id(consumer)].add(id(producer))
    edges = {key: set(hard[key]) | soft[key] for key in hard}
    group = _cycle_groups([id(site) for site in nodes], edges)
    rank = {id(site): index for index, site in enumerate(sites)}
    members: dict[int, list[int]] = {}
    for key in sorted(group, key=lambda item: rank[item]):
        members.setdefault(group[key], []).append(key)
    waiting = {
        number: {group[p] for key in keys for p in edges[key] if group[p] != number}
        for number, keys in members.items()
    }
    order: list[int] = []
    placed: set[int] = set()
    while len(placed) < len(members):
        ready = [number for number in members if number not in placed and waiting[number] <= placed]
        if not ready:  # cannot happen once cycles are grouped; keep the given order
            ready = [number for number in members if number not in placed]
        best = min(ready, key=lambda number: rank[members[number][0]])
        placed.add(best)
        order.extend(members[best])
    if routes is not None:
        for key in hard:
            outside = [p for p in hard[key] if group[p] != group[key]]
            if outside:
                routes.feeders[key] = sorted(outside, key=lambda item: order.index(item))
            for p, kind in hard[key].items():
                routes.edge_kind[(key, p)] = kind
            routes.producers[key] = sorted(edges[key], key=lambda item: order.index(item))
    by_key = {id(site): site for site in nodes}
    out = [by_key[key] for key in order]
    # Sites outside the graph (covered lines, wrappers, unparsable) keep their place after.
    out += [site for site in sites if id(site) not in by_key]
    return out


def _cycle_groups(keys: list[int], edges: dict[int, set[int]]) -> dict[int, int]:
    """Strongly connected components (Tarjan): key -> group number."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on: set[int] = set()
    group: dict[int, int] = {}
    counter = [0, 0]

    def visit(key: int) -> None:
        index[key] = low[key] = counter[0]
        counter[0] += 1
        stack.append(key)
        on.add(key)
        for other in edges.get(key, ()):
            if other not in index:
                visit(other)
                low[key] = min(low[key], low[other])
            elif other in on:
                low[key] = min(low[key], index[other])
        if low[key] == index[key]:
            while True:
                item = stack.pop()
                on.discard(item)
                group[item] = counter[1]
                if item == key:
                    break
            counter[1] += 1

    for key in keys:
        if key not in index:
            visit(key)
    return group


def _used_in_span(tree: ast.Module, span: tuple[int, int]) -> set[tuple[str, int]]:
    """Names loaded in ``span`` (string annotations included), with their lines."""
    return {
        (name, line)
        for name, lines in _name_lines(tree).items()
        for line in lines
        if span[0] <= line <= span[1]
    }


def _name_lines(tree: ast.Module) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}

    def add(node: ast.AST, line: int) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                out.setdefault(sub.id, set()).add(line or sub.lineno)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and line:
                try:
                    add(ast.parse(sub.value.strip(), mode="eval"), line)
                except SyntaxError:
                    pass

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.setdefault(node.id, set()).add(node.lineno)
        for annotation in _annotations_of(node):
            add(annotation, annotation.lineno)
    return out


def _annotations_of(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.arg) and node.annotation is not None:
        return [node.annotation]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
        return [node.returns]
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    return []


def _with_received_types(config: ApplyConfig, site: ImpactSite, uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the new-release type an accepted rewrite now passes to each annotated use."""
    routes = config.routes
    if routes is None or not uses:
        return uses
    out: list[dict[str, Any]] = []
    for use in uses:
        item = dict(use)
        for key, sink in routes.accepted.items():
            if key == routes.key or "now_receives" in item:
                continue
            for flow in sink.get("flows") or []:
                if (
                    item.get("param")
                    and flow.get("callee") == item.get("function")
                    and flow.get("param") == item["param"]
                    and _path_match(flow.get("callee_path", ""), site.path)
                ):
                    item["now_receives"] = f"{flow['types']} ({flow['release']}), passed from {flow['from']}"
                    if flow.get("imports"):
                        item["import_for_it"] = list(flow["imports"])
                    break
        out.append(item)
    return out


def _producer_rewrites(config: ApplyConfig, pack: ContextPack) -> list[dict[str, Any]]:
    """Finished new code of the sites this one depends on, not already shown."""
    routes = config.routes
    if routes is None:
        return []
    shown = {item.get("rewritten_code") for item in pack.connected}
    out: list[dict[str, Any]] = []
    for other in routes.producers.get(routes.key, []):
        sink = routes.accepted.get(other)
        if sink is None or other == routes.key:
            continue
        block = sink.get("block") or ""
        if not block or block in shown:
            continue
        item: dict[str, Any] = {
            "site": f"{sink['site'].path}:{sink['site'].line}",
            "rewritten_code": block,
        }
        edits = getattr(sink.get("edit"), "line_edits", ()) or ()
        if edits:
            item["also_changed_lines"] = [new.strip() for _line, _old, new in edits]
        out.append(item)
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
    except Exception:  # noqa: BLE001 — extra context only; never blocks a fix
        return {}


_MAX_REQUIRES = 2
# A recheck finding from ``typecheck.argument_breaks``: ``path:line expr: kw= passes ...``.
_MISMATCH = re.compile(r"^(?P<path>\S+?):\d+ .*?: (?P<kw>\w+)= passes .*? but (?P<owner>[\w.]+) in ")


def _type_mismatches(findings: list[str]) -> set[tuple[str, str, str]]:
    """``(path, keyword, callable)`` of each argument-type finding (lines move between attempts)."""
    out: set[tuple[str, str, str]] = set()
    for text in findings:
        match = _MISMATCH.match(text or "")
        if match:
            out.add((match["path"], match["kw"], match["owner"]))
    return out


def _required_parameters(config: ApplyConfig, site: ImpactSite, symbols: list[str]) -> list[str]:
    """For new-release callables: required parameters with their annotated types
    (TypedDict structure expanded) and the other names they accept.

    Distinct texts only (an async twin or re-export reads the same), at most
    ``_MAX_REQUIRES``."""
    symbols = [symbol for symbol in symbols if symbol]
    if not config.recheck_impact or not symbols:
        return []
    try:
        wheel = new_release_wheel(
            site.dependency, site.to_version, fixtures=config.fixtures, allow_network=config.allow_network
        )
        if wheel is None:
            return []
        modules = _modules(str(wheel))
        out: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            brief = parameter_brief(modules, symbol)
            body = brief.split(" requires: ", 1)[-1]
            if not brief or body in seen:
                continue
            seen.add(body)
            out.append(brief)
            if len(out) >= _MAX_REQUIRES:
                break
        return out
    except Exception:  # noqa: BLE001 — extra context only; never blocks a fix
        return []


def _repo_helpers(
    config: ApplyConfig, site: ImpactSite, span: tuple[int, int] | None, symbols: list[str]
) -> list[dict]:
    """Repo functions that already return a structured type the new callable requires."""
    symbols = [symbol for symbol in symbols if symbol]
    if not config.recheck_impact or not symbols:
        return []
    try:
        wheel = new_release_wheel(
            site.dependency, site.to_version, fixtures=config.fixtures, allow_network=config.allow_network
        )
        if wheel is None:
            return []
        modules = _modules(str(wheel))
        requirements: list[dict] = []
        method = ""
        for symbol in symbols:
            found = structured_requirements(modules, symbol)
            if not found:
                continue
            method = method or symbol.rsplit(".", 1)[-1]
            for req in found:
                same = next((r for r in requirements if r["param"] == req["param"]), None)
                if same is None:
                    requirements.append(dict(req))
                else:
                    same["names"] = sorted(set(same["names"]) | set(req["names"]))
        exclude = (site.path, span[0], span[1]) if span and span[0] else None
        return repo_producers(Path(config.root), requirements, method, exclude)
    except Exception:  # noqa: BLE001 — extra context only; never blocks a fix
        return []


def _constant_values(config: ApplyConfig, site: ImpactSite) -> dict[str, str]:
    """The literal a flagged constant held in the old release, and in the new one if it changed.

    A removed constant otherwise reaches the prompt as a bare name; its old
    value is what a faithful inline replacement has to reproduce."""
    if not config.recheck_impact or not site.symbol or not site.from_version:
        return {}
    try:
        old_wheel = new_release_wheel(
            site.dependency, site.from_version, fixtures=config.fixtures, allow_network=config.allow_network
        )
        old = literal_value(_modules(str(old_wheel)), site.symbol) if old_wheel is not None else ""
        if not old:
            return {}
        new_wheel = new_release_wheel(
            site.dependency, site.to_version, fixtures=config.fixtures, allow_network=config.allow_network
        )
        new = literal_value(_modules(str(new_wheel)), site.symbol) if new_wheel is not None else ""
    except Exception:  # noqa: BLE001 — extra context only; never blocks a fix
        return {}
    if new == old:
        return {}
    out = {"old_value": f"{site.symbol} = {old}"}
    if new:
        out["new_value"] = f"{site.symbol} = {new}"
    return out


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
