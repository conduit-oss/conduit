"""Conduit CLI — autonomous API migration engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from conduit.context.fetch import read_local_text
from conduit.detect.coverage import (
    PacketCoverageReport,
    build_coverage_report,
    format_coverage_report,
    save_source_packet,
)
from conduit.detect.modules.discovery import load_modules
from conduit.detect.orchestrator import run_detect
from conduit.export_delta import compute_export_delta, prune_by_export_symbols
from conduit.packet.cache import save_packet
from conduit.packet.synthesize import (
    ensure_packet,
    load_fixture_openai_packet,
    synthesize_from_docs,
)
from conduit.packet.validate import validate_packet
from conduit.patcher import apply_packet
from conduit.patcher.dependency_update import dependency_packages
from conduit.pr_generator import open_pull_request
from conduit.prune.grep_imports import prune_by_imports
from conduit.pulse import beat, start_pulse, stop_pulse
from conduit.run_summary import build_run_summary, format_run_summary, format_run_summary_markdown
from conduit.scaffold.module_new import scaffold_module
from conduit.scaffold.packet_init import scaffold_packet
from conduit.self_correct import verify_with_self_correct
from conduit.test_gen import ensure_tests

app = typer.Typer(
    name="conduit",
    help="Autonomous breaking-API migration CLI",
    add_completion=False,
)
module_app = typer.Typer(help="Detect module tools")
packet_app = typer.Typer(help="Migration packet tools")
app.add_typer(module_app, name="module")
app.add_typer(packet_app, name="packet")
console = Console()
_VERBOSE = False


def _vprint(message: str) -> None:
    if _VERBOSE:
        console.print(f"[dim][verbose][/dim] {message}")


def _print_packet_coverage(
    *,
    root: Path,
    package: str,
    detected,
    packet: dict | None = None,
    persist_source: bool = True,
) -> PacketCoverageReport:
    """Print source packet, migration summary, and caught/missed coverage diff."""
    state = (detected.package_states or {}).get(package) or (
        detected.package_states or {}
    ).get(package.lower())
    report = build_coverage_report(
        package=package,
        state=state,
        signals=detected.signals,
        packet=packet,
    )
    console.print(format_coverage_report(report, verbose=_VERBOSE))
    if report.missed:
        console.print(
            f"[yellow]Coverage:[/yellow] {len(report.missed)} client item(s) not "
            "covered by migration signals/rules (see MISSED above)."
        )
    if persist_source:
        path = save_source_packet(root, report.source_packet)
        _vprint(f"wrote source packet {path}")
    return report


def _make_run_summary(
    *,
    packet: dict,
    report,
    test_result,
    coverage: PacketCoverageReport | None,
    detected,
    generated: list[str],
    corrected: list[str],
    skip_tests: bool = False,
    pr_created: bool | None = None,
    pr_message: str | None = None,
):
    package = str(packet.get("package") or "")
    state = None
    if detected is not None and package:
        state = (detected.package_states or {}).get(package) or (
            detected.package_states or {}
        ).get(package.lower())
    return build_run_summary(
        packet=packet,
        report=report,
        test_result=test_result,
        coverage=coverage,
        state=state,
        generated=generated,
        corrected=corrected,
        skip_tests=skip_tests,
        pr_created=pr_created,
        pr_message=pr_message,
        detected_signals=list(getattr(detected, "signals", None) or []),
    )


def _print_run_summary(
    *,
    packet: dict,
    report,
    test_result,
    coverage: PacketCoverageReport | None,
    detected,
    generated: list[str],
    corrected: list[str],
    skip_tests: bool = False,
    pr_created: bool | None = None,
    pr_message: str | None = None,
) -> str:
    """Print the changed / double-check summary. Returns markdown for the PR body."""
    summary = _make_run_summary(
        packet=packet,
        report=report,
        test_result=test_result,
        coverage=coverage,
        detected=detected,
        generated=generated,
        corrected=corrected,
        skip_tests=skip_tests,
        pr_created=pr_created,
        pr_message=pr_message,
    )
    console.print(format_run_summary(summary))
    return format_run_summary_markdown(summary)


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print extra diagnostics"
    ),
) -> None:
    global _VERBOSE
    _VERBOSE = verbose


def _verify_with_oracle(
    root: Path,
    packet: dict,
    *,
    max_retries: int,
    changed_files: list[str] | None = None,
    file_allowlist: list[Path] | None = None,
    source: dict | None = None,
    coverage_missed: list[dict] | None = None,
):
    """Write packet oracle tests, then run the suite with self-correct.

    Shared by ``conduit verify`` and ``conduit run`` so split workflows
    get the same leftover-token checks.
    """
    pkg = str(packet.get("package") or "")
    allowlist = file_allowlist
    if allowlist is None and pkg:
        allowlist = prune_by_imports(root, dependency_packages(packet))

    beat("hatch")
    generated = ensure_tests(
        root,
        packet,
        changed_files=changed_files,
        file_allowlist=allowlist,
    )
    for rel in generated:
        console.print(f"[test-gen] created {rel}")

    beat("repair")
    result, corrected = verify_with_self_correct(
        root,
        packet,
        max_retries=max_retries,
        verbose=_VERBOSE,
        log=console.print,
        source=source,
        coverage_missed=coverage_missed,
    )
    return result, generated, corrected


def _resolve_root(path: Path) -> Path:
    raw = str(path)
    # Windows: "C:foo" (no slash after colon) is drive-relative to cwd, not
    # "C:\foo". That usually means backslashes were eaten by the shell.
    if len(raw) >= 3 and raw[1] == ":" and raw[2] not in "\\/":
        console.print(f"[red]Invalid Windows path:[/red] {raw}")
        console.print(
            "Backslashes were likely stripped. Use forward slashes or quotes, e.g.\n"
            '  --path "C:/Users/you/project"\n'
            "Or if you are already in the repo:\n"
            "  --path ."
        )
        raise typer.Exit(2)
    root = path.expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]Path is not a directory:[/red] {root}")
        raise typer.Exit(2)
    return root


def _pick_package(signals, package: Optional[str]) -> str | None:
    if package:
        return package
    packages = sorted({s.package for s in signals if s.package})
    if not packages:
        return None
    wanted = {p.lower() for p in packages}
    try:
        from conduit.detect.modules.discovery import load_modules

        for mod in load_modules():
            for p in list(mod.packages or []) + [mod.name]:
                if p.lower() in wanted:
                    for orig in packages:
                        if orig.lower() == p.lower():
                            return orig
    except Exception:
        pass
    return packages[0]


def _resolve_packet_arg(
    packet: Optional[str],
) -> tuple[Optional[Path], Optional[str]]:
    """
    Interpret --packet as an existing packet file path, or else a package name.
    Returns (packet_file, package_name).
    """
    if not packet:
        return None, None
    raw = packet.strip()
    if not raw:
        return None, None
    as_path = Path(raw).expanduser()
    if as_path.is_file():
        return as_path.resolve(), None
    # Bare package names must not look like accidental relative paths with separators
    if any(sep in raw for sep in ("/", "\\")) or raw.endswith(".json"):
        console.print(f"[red]Packet file not found:[/red] {raw}")
        raise typer.Exit(2)
    return None, raw


def _detect_module_names_for_package(package: str | None) -> list[str] | None:
    if not package:
        return None
    available = {m.name.lower() for m in load_modules()}
    if package.lower() in available:
        return [package]
    return None


@app.command("detect")
def detect_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    base_ref: Optional[str] = typer.Option(None, "--base-ref"),
    module: Optional[str] = typer.Option(
        None, "--module", help="Only run this detect module (e.g. openai)"
    ),
    skip_modules: bool = typer.Option(False, "--skip-modules"),
    skip_lockfile: bool = typer.Option(False, "--skip-lockfile"),
    majors_only: bool = typer.Option(True, "--majors-only/--all-bumps"),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Use offline detect fixtures (default: live vendor sources)",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run lockfile diff + vendor detect modules."""
    root = _resolve_root(path)
    names = [module] if module else None
    if not json_out:
        start_pulse(console, "detect")
    try:
        result = run_detect(
            root,
            base_ref=base_ref,
            majors_only=majors_only,
            module_names=names,
            skip_modules=skip_modules,
            skip_lockfile=skip_lockfile,
            demo=demo,
            verbose=_VERBOSE,
            log=None if json_out else console.print,
        )
    finally:
        stop_pulse()
    if json_out:
        payload = {
            "signals": [s.to_dict() for s in result.signals],
            "package_states": {
                k: v.to_dict() for k, v in (result.package_states or {}).items()
            },
        }
        # Include coverage when a primary package is obvious
        pkgs = sorted(result.packages | set(result.package_states or {}))
        if pkgs:
            from conduit.detect.coverage import build_coverage_report

            pkg0 = _pick_package(result.signals, None) or pkgs[0]
            state = (result.package_states or {}).get(pkg0)
            payload["coverage"] = build_coverage_report(
                package=pkg0,
                state=state,
                signals=result.signals,
                packet=None,
            ).to_dict()
        console.print_json(json.dumps(payload))
        raise typer.Exit(0 if result.signals else 1)

    table = Table(title=f"Detect signals in {root}")
    table.add_column("Source")
    table.add_column("Package")
    table.add_column("Type")
    table.add_column("Detail")
    for s in result.signals:
        detail = s.description or s.affected_pattern or ""
        if s.from_version and s.to_version:
            detail = f"{s.from_version} -> {s.to_version}"
        table.add_row(s.source, s.package, s.change_type, detail[:80])
    console.print(table)
    console.print(f"[bold]{len(result.signals)}[/bold] signal(s).")
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")

    # Source packet + coverage vs signals (migration packet not built yet on detect)
    pkgs = sorted(result.packages | set(result.package_states or {}))
    if pkgs:
        pkg0 = _pick_package(result.signals, None) or pkgs[0]
        # normalize to actual key
        for key in result.package_states or {}:
            if key.lower() == pkg0.lower():
                pkg0 = key
                break
        _print_packet_coverage(
            root=root,
            package=pkg0,
            detected=result,
            packet=None,
            persist_source=True,
        )

    raise typer.Exit(0 if result.signals else 1)


@app.command("apply")
def apply_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    packet: Path = typer.Option(..., "--packet", help="Path to conduit-packet.json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Apply a Migration Packet without opening a PR."""
    root = _resolve_root(path)
    data = json.loads(packet.read_text(encoding="utf-8"))
    errors = validate_packet(data)
    if errors:
        for err in errors:
            console.print(f"[red]schema:[/red] {err}")
        raise typer.Exit(1)
    files = prune_by_imports(root, dependency_packages(data))
    report = apply_packet(root, data, dry_run=dry_run, file_allowlist=files or None)
    for change in report.changes:
        prefix = "DRY-RUN " if dry_run else ""
        console.print(f"{prefix}[{change.rule_type}] {change.path}: {change.detail}")
    console.print(
        f"{'Would modify' if dry_run else 'Modified'} "
        f"{len(report.files_modified)} file(s)."
    )


@app.command("verify")
def verify_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    packet: Optional[Path] = typer.Option(None, "--packet"),
    max_retries: int = typer.Option(5, "--max-retries"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print self-correct failure/fix details"
    ),
) -> None:
    """Run oracle tests + native suite with optional self-correction."""
    global _VERBOSE
    if verbose:
        _VERBOSE = True
    root = _resolve_root(path)
    data = (
        json.loads(packet.read_text(encoding="utf-8"))
        if packet
        else load_fixture_openai_packet()
    )
    start_pulse(console, "repair")
    try:
        result, _generated, corrected = _verify_with_oracle(
            root, data, max_retries=max_retries
        )
    finally:
        stop_pulse()
    for rel in corrected:
        console.print(f"[self-correct] updated {rel}")
    console.print(result.summary)
    raise typer.Exit(0 if result.passed else 2)


@app.command("run")
def run_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    base_ref: Optional[str] = typer.Option(None, "--base-ref"),
    package: Optional[str] = typer.Option(None, "--package"),
    module: Optional[str] = typer.Option(None, "--module"),
    packet: Optional[str] = typer.Option(
        None,
        "--packet",
        help="Path to conduit-packet.json, or a package name (e.g. openai)",
    ),
    skip_tests: bool = typer.Option(False, "--skip-tests"),
    skip_pr: bool = typer.Option(False, "--skip-pr"),
    no_push: bool = typer.Option(False, "--no-push"),
    skip_modules: bool = typer.Option(False, "--skip-modules"),
    skip_lockfile: bool = typer.Option(False, "--skip-lockfile"),
    skip_export_delta: bool = typer.Option(
        False, "--skip-export-delta", help="Skip package export delta pruning"
    ),
    max_retries: int = typer.Option(5, "--max-retries"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print extra diagnostics"
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Use offline detect fixtures + openai demo packet fallback (default: live)",
    ),
    refresh_packet: bool = typer.Option(
        False,
        "--refresh-packet",
        help="Ignore cached .conduit/packets entry and re-synthesize from detect signals",
    ),
) -> None:
    """Full pipeline: detect → prune → packet → apply → verify → PR."""
    global _VERBOSE
    if verbose:
        _VERBOSE = True
    root = _resolve_root(path)
    packet_file, packet_package = _resolve_packet_arg(packet)
    start_pulse(console, "awakening")
    try:
        _run_pipeline(
            root=root,
            packet_file=packet_file,
            packet_package=packet_package,
            package=package,
            module=module,
            base_ref=base_ref,
            skip_tests=skip_tests,
            skip_pr=skip_pr,
            no_push=no_push,
            skip_modules=skip_modules,
            skip_lockfile=skip_lockfile,
            skip_export_delta=skip_export_delta,
            max_retries=max_retries,
            demo=demo,
            refresh_packet=refresh_packet,
        )
    finally:
        stop_pulse()


def _run_pipeline(
    *,
    root: Path,
    packet_file,
    packet_package,
    package,
    module,
    base_ref,
    skip_tests: bool,
    skip_pr: bool,
    no_push: bool,
    skip_modules: bool,
    skip_lockfile: bool,
    skip_export_delta: bool,
    max_retries: int,
    demo: bool,
    refresh_packet: bool,
) -> None:
    pkg_hint = package or packet_package
    if package and packet_package and package.lower() != packet_package.lower():
        console.print(
            f"[yellow]Warning:[/yellow] --package {package!r} differs from "
            f"--packet package name {packet_package!r}; using --package"
        )

    names = [module] if module else _detect_module_names_for_package(pkg_hint)
    if demo:
        console.print("[dim]Demo mode: using offline detect fixtures[/dim]")
    beat("detect")
    detected = run_detect(
        root,
        base_ref=base_ref,
        module_names=names,
        skip_modules=skip_modules,
        skip_lockfile=skip_lockfile,
        demo=demo,
        verbose=_VERBOSE,
        log=console.print,
    )
    for warning in detected.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    by_type: dict[str, int] = {}
    for s in detected.signals:
        by_type[s.change_type] = by_type.get(s.change_type, 0) + 1
    console.print(
        "detect signals: "
        + (", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "(none)")
    )
    pkg = _pick_package(detected.signals, pkg_hint)

    if packet_file is not None:
        pkt_data = json.loads(packet_file.read_text(encoding="utf-8"))
        file_pkg = str(pkt_data.get("package") or "")
        if package and file_pkg and package.lower() != file_pkg.lower():
            console.print(
                f"[yellow]Warning:[/yellow] --package {package!r} differs from "
                f"packet file package {file_pkg!r}; using packet file"
            )
        pkg = file_pkg or pkg
        if not pkg:
            console.print("[red]Packet file has no package field.[/red]")
            raise typer.Exit(2)
        beat("packet")
        state = (detected.package_states or {}).get(pkg) or (
            detected.package_states or {}
        ).get((pkg or "").lower())
        ensured = ensure_packet(
            root,
            detected.signals,
            package=pkg,
            packet_path=packet_file,
            installed=detected.installed,
            use_fixture_fallback=demo,
            refresh=refresh_packet,
            client_state=state,
            log=console.print,
        )
    else:
        if pkg is None:
            pkg = _pick_package(detected.signals, None)
            if not pkg:
                console.print("[green]No migration signals found. Nothing to do.[/green]")
                raise typer.Exit(0)
        beat("packet")
        state = (detected.package_states or {}).get(pkg) or (
            detected.package_states or {}
        ).get((pkg or "").lower())
        ensured = ensure_packet(
            root,
            detected.signals,
            package=pkg,
            packet_path=None,
            installed=detected.installed,
            use_fixture_fallback=demo,
            refresh=refresh_packet,
            client_state=state,
            log=console.print,
        )

    pkt = ensured.packet
    for warning in ensured.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    console.print(f"Using packet [bold]{pkt.get('packet_id')}[/bold] ({len(pkt.get('rules') or [])} rules)")
    _vprint(
        f"packet from_version={pkt.get('from_version')!r} ({ensured.from_source}) "
        f"to_version={pkt.get('to_version')!r} ({ensured.to_source}) "
        f"ecosystem={pkt.get('ecosystem')!r}"
    )

    # Always print source packet + migration packet + coverage diff
    coverage = _print_packet_coverage(
        root=root,
        package=pkg,
        detected=detected,
        packet=pkt,
        persist_source=True,
    )

    beat("prune")
    pkgs = dependency_packages(pkt)
    files = prune_by_imports(root, pkgs)
    console.print(
        f"Pruned to {len(files)} file(s) importing {', '.join(pkgs)}"
    )

    if not skip_export_delta:
        from_v = str(pkt.get("from_version") or "")
        to_v = str(pkt.get("to_version") or "")
        eco = str(pkt.get("ecosystem") or "pypi")
        beat("export")
        _vprint(f"export delta resolve {pkg} {from_v} -> {to_v} ({eco})")
        delta = compute_export_delta(
            package=pkg,
            from_version=from_v,
            to_version=to_v,
            ecosystem=eco,
            cache_root=root / ".conduit" / "exports",
        )
        if delta.skipped_reason:
            console.print(f"[yellow]Export delta skipped:[/yellow] {delta.skipped_reason}")
            for line in delta.diagnostics:
                _vprint(line)
        else:
            before = len(files)
            files = prune_by_export_symbols(files, delta)
            console.print(
                f"Export delta: {len(delta.removed)} removed, {len(delta.added)} added, "
                f"{len(delta.renamed)} renamed → {len(files)} file(s) (was {before})"
            )
            _vprint(
                f"symbols from={len(delta.from_symbols)} to={len(delta.to_symbols)} "
                f"changed={len(delta.changed_symbols)}"
            )

    beat("apply")
    report = apply_packet(root, pkt, dry_run=False, file_allowlist=files or None)
    for change in report.changes:
        console.print(f"[{change.rule_type}] {change.path}: {change.detail}")

    if skip_tests:
        from conduit.test_runner import TestResult

        test_result = TestResult(
            runner="skipped",
            passed=True,
            returncode=0,
            stdout="skipped",
            stderr="",
            command=[],
        )
        generated: list[str] = []
        corrected: list[str] = []
    else:
        src_state = (detected.package_states or {}).get(pkg) or (
            detected.package_states or {}
        ).get((pkg or "").lower())
        test_result, generated, corrected = _verify_with_oracle(
            root,
            pkt,
            max_retries=max_retries,
            changed_files=report.files_modified,
            file_allowlist=files,
            source=src_state.to_dict() if src_state is not None else None,
            coverage_missed=(
                [
                    {"kind": i.kind, "value": i.value, "detail": i.detail}
                    for i in coverage.missed
                ]
                if coverage is not None
                else None
            ),
        )
        for rel in generated:
            if rel not in report.files_modified:
                report.files_modified.append(rel)
        for rel in corrected:
            console.print(f"[self-correct] updated {rel}")
            if rel not in report.files_modified:
                report.files_modified.append(rel)

    console.print(test_result.summary)
    if not test_result.passed:
        console.print("[red]Tests still failing after self-correction; aborting PR.[/red]")
        if test_result.stdout:
            console.print(test_result.stdout[-2000:])
        if test_result.stderr:
            console.print(test_result.stderr[-2000:])
        _print_run_summary(
            packet=pkt,
            report=report,
            test_result=test_result,
            coverage=coverage,
            detected=detected,
            generated=generated,
            corrected=corrected,
            skip_tests=skip_tests,
        )
        raise typer.Exit(2)

    if skip_pr:
        console.print("[green]Patches applied and tests passed (PR skipped).[/green]")
        _print_run_summary(
            packet=pkt,
            report=report,
            test_result=test_result,
            coverage=coverage,
            detected=detected,
            generated=generated,
            corrected=corrected,
            skip_tests=skip_tests,
            pr_created=None,
            pr_message="PR skipped (--skip-pr)",
        )
        raise typer.Exit(0)

    detect_summary = "\n".join(
        f"- [{s.source}] {s.package} {s.change_type}: "
        f"{s.description or s.affected_pattern or ''}"
        for s in detected.signals[:20]
    )
    review_markdown = format_run_summary_markdown(
        _make_run_summary(
            packet=pkt,
            report=report,
            test_result=test_result,
            coverage=coverage,
            detected=detected,
            generated=generated,
            corrected=corrected,
            skip_tests=skip_tests,
        )
    )
    beat("pr")
    pr = open_pull_request(
        root,
        pkt,
        report,
        test_result,
        push=not no_push,
        create_pr=True,
        detect_summary=detect_summary,
        review_markdown=review_markdown,
    )
    console.print(pr.message)
    _print_run_summary(
        packet=pkt,
        report=report,
        test_result=test_result,
        coverage=coverage,
        detected=detected,
        generated=generated,
        corrected=corrected,
        skip_tests=skip_tests,
        pr_created=pr.created,
        pr_message=pr.message,
    )
    raise typer.Exit(0 if pr.created or skip_pr else 3)


@module_app.command("list")
def module_list_cmd(
    path: Path = typer.Option(Path("."), "--path"),
) -> None:
    """List built-in and entry-point detect modules."""
    from conduit.detect.manifests import read_installed

    root = _resolve_root(path)
    installed = read_installed(root)
    table = Table(title="Detect modules")
    table.add_column("Name")
    table.add_column("Packages")
    table.add_column("Applies")
    for mod in load_modules():
        applies = mod.applies(installed) if installed else "n/a (no manifests)"
        table.add_row(mod.name, ", ".join(mod.packages), str(applies))
    console.print(table)


@module_app.command("new")
def module_new_cmd(
    name: str = typer.Argument(..., help="Module name, e.g. stripe"),
    package: Optional[str] = typer.Option(None, "--package"),
    ecosystem: str = typer.Option("pypi", "--ecosystem"),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Conduit package root (contains src/conduit) or out-of-tree dir",
    ),
    out_of_tree: bool = typer.Option(
        False, "--out-of-tree", help="Scaffold a standalone module package"
    ),
    deprecations_url: Optional[str] = typer.Option(None, "--deprecations-url"),
    changelog_url: Optional[str] = typer.Option(None, "--changelog-url"),
    openapi_repo: Optional[str] = typer.Option(None, "--openapi-repo"),
    sdk_repo: Optional[str] = typer.Option(None, "--sdk-repo"),
    catalog_url: Optional[str] = typer.Option(None, "--catalog-url"),
    model_doc_template: Optional[str] = typer.Option(None, "--model-doc-template"),
    live_catalog_url: Optional[str] = typer.Option(None, "--live-catalog-url"),
    live_catalog_auth_env: Optional[str] = typer.Option(None, "--live-catalog-auth-env"),
    evidence_hosts: Optional[str] = typer.Option(None, "--evidence-hosts"),
) -> None:
    """Scaffold a profile-backed detect module (prompts for source URLs on a TTY)."""
    target = _resolve_root(path)
    # Prefer conduit package root when invoked from monorepo
    pkg_root = target / "conduit" if (target / "conduit" / "src" / "conduit").is_dir() else target
    if (pkg_root / "src" / "conduit").is_dir():
        target = pkg_root
    mod_dir = scaffold_module(
        name,
        package=package,
        ecosystem=ecosystem,
        target_root=target,
        out_of_tree=out_of_tree,
        deprecations_url=deprecations_url,
        changelog_url=changelog_url,
        openapi_repo=openapi_repo,
        sdk_repo=sdk_repo,
        catalog_url=catalog_url,
        model_doc_template=model_doc_template,
        live_catalog_url=live_catalog_url,
        live_catalog_auth_env=live_catalog_auth_env,
        evidence_hosts=evidence_hosts,
    )
    console.print(f"[green]Created profile-backed module at[/green] {mod_dir}")
    console.print("Next: fill profile.py / custom parsers, then `conduit module list`.")


@packet_app.command("init")
def packet_init_cmd(
    package: str = typer.Option(..., "--package"),
    from_version: str = typer.Option(..., "--from"),
    to_version: str = typer.Option(..., "--to"),
    ecosystem: str = typer.Option("pypi", "--ecosystem"),
    out: Path = typer.Option(Path("."), "--out"),
) -> None:
    """Scaffold a vendor Migration Packet directory."""
    out_dir = out
    if out_dir.exists() and out_dir.is_dir() and (out_dir / "conduit-packet.json").exists():
        pass
    elif out.name.endswith(".json"):
        out_dir = out.parent
    else:
        # default nested dir name
        if out == Path("."):
            out_dir = Path(f"{package}-{from_version}-{to_version}")
    path = scaffold_packet(
        package=package,
        ecosystem=ecosystem,
        from_version=from_version,
        to_version=to_version,
        out_dir=out_dir,
    )
    console.print(f"[green]Created[/green] {path}")


@packet_app.command("validate")
def packet_validate_cmd(
    packet: Path = typer.Argument(..., help="Path to conduit-packet.json"),
) -> None:
    """Validate a packet against the public schema."""
    data = json.loads(packet.read_text(encoding="utf-8"))
    errors = validate_packet(data)
    if errors:
        for err in errors:
            console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    console.print("[green]Packet is valid.[/green]")


@packet_app.command("show")
def packet_show_cmd(
    packet: Path = typer.Argument(...),
) -> None:
    """Pretty-print a packet."""
    data = json.loads(packet.read_text(encoding="utf-8"))
    console.print_json(json.dumps(data))


@packet_app.command("synthesize")
def packet_synthesize_cmd(
    package: str = typer.Option(..., "--package"),
    from_version: str = typer.Option(..., "--from"),
    to_version: str = typer.Option(..., "--to"),
    ecosystem: str = typer.Option("pypi", "--ecosystem"),
    changelog: Optional[Path] = typer.Option(None, "--changelog"),
    docs: Optional[Path] = typer.Option(None, "--docs"),
    out: Path = typer.Option(Path("conduit-packet.json"), "--out"),
) -> None:
    """Synthesize a packet from changelog/docs (LLM if configured)."""
    packet = synthesize_from_docs(
        package=package,
        from_version=from_version,
        to_version=to_version,
        ecosystem=ecosystem,
        changelog_text=read_local_text(changelog),
        docs_text=read_local_text(docs),
    )
    save_packet(out, packet)
    errors = validate_packet(packet)
    if errors:
        console.print("[yellow]Wrote packet with schema warnings:[/yellow]")
        for err in errors:
            console.print(f"  {err}")
    else:
        console.print(f"[green]Wrote valid packet[/green] {out}")


@packet_app.command("from-detect")
def packet_from_detect_cmd(
    module: str = typer.Option(..., "--module", help="Detect module (e.g. openai)"),
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Directory for snapshot JSON files"),
    package: Optional[str] = typer.Option(None, "--package"),
    ecosystem: Optional[str] = typer.Option(
        None, "--ecosystem", help="Write only this chain (pypi, npm, go, maven)"
    ),
    previous: Optional[Path] = typer.Option(
        None, "--previous", help="Previous snapshot JSON (requires --ecosystem)"
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Explicit output file (requires --ecosystem)"
    ),
    demo: bool = typer.Option(False, "--demo", help="Offline detect fixtures"),
    enrich: bool = typer.Option(
        False, "--enrich", help="Optional LLM rule pass (off = scrape only)"
    ),
) -> None:
    """Freeze catalog snapshot packets from detect (no consumer repo)."""
    from conduit.packet.from_detect import run_packet_from_detect

    if previous is not None and not ecosystem:
        console.print("[red]--previous requires --ecosystem[/red]")
        raise typer.Exit(2)
    if out is not None and not ecosystem:
        console.print("[red]--out requires --ecosystem[/red]")
        raise typer.Exit(2)
    if ecosystem and ecosystem.lower() not in {"pypi", "npm", "go", "maven", "other"}:
        console.print(f"[red]Unknown ecosystem {ecosystem!r}[/red]")
        raise typer.Exit(2)

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        writes, warnings = run_packet_from_detect(
            module=module,
            out_dir=out_dir,
            package=package,
            ecosystem=ecosystem.lower() if ecosystem else None,
            previous_path=previous,
            out_path=out,
            demo=demo,
            enrich=enrich,
            log=console.print,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    for warning in warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")

    if not writes:
        console.print("[red]Scan produced no SDK target version (nothing to write).[/red]")
        raise typer.Exit(2)

    wrote = 0
    for item in writes:
        pkt = item.packet
        pkg = pkt.get("package")
        eco = pkt.get("ecosystem")
        to_v = pkt.get("to_version")
        from_v = pkt.get("from_version")
        console.print(f"target {pkg} {to_v} ({eco})")
        if from_v and from_v != "0":
            console.print(f"previous snapshot {from_v}")
        else:
            console.print("previous snapshot (none)")
        n_rules = len(pkt.get("rules") or [])
        if item.skipped:
            console.print(f"[dim]skip[/dim] {item.path} ({item.skip_reason})")
        else:
            wrote += 1
            console.print(f"[green]wrote[/green] {item.path}  ({n_rules} rules)")

    if wrote == 0:
        console.print("[dim]All snapshot chains already up to date.[/dim]")


if __name__ == "__main__":
    app()
