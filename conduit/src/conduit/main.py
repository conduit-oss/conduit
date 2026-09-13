"""Conduit CLI — autonomous API migration engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from conduit.context.fetch import read_local_text
from conduit.credentials import (
    CredentialsError,
    ensure_verify_credentials,
    load_consumer_env,
)
from conduit.run_preflight import collect_run_preflight_warnings, print_run_preflight
from conduit.detect.coverage import (
    PacketCoverageReport,
    build_coverage_report,
    format_coverage_report,
    load_source_packet,
    save_source_packet,
)
from conduit.detect.modules.discovery import load_modules
from conduit.detect.manifests import (
    pin_for_packet_ecosystem,
    read_installed_by_ecosystem,
)
from conduit.detect.orchestrator import run_detect
from conduit.export_delta import compute_export_delta, prune_by_export_symbols
from conduit.packet.cache import save_packet
from conduit.packet.fetch import PacketFetchError, fetch_packet_url, is_packet_url
from conduit.packet.synthesize import (
    ensure_packet,
    load_fixture_openai_packet,
    synthesize_from_docs,
)
from conduit.packet.bind import bind_packet_to_client, is_snapshot_floor
from conduit.packet.scope import scope_packet_to_source
from conduit.packet.validate import validate_packet
from conduit.patcher import apply_packet
from conduit.patcher.rule_stages import partition_rules
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


def _ecosystem_client_pin(
    root: Path,
    packet: dict,
    *,
    fallback: str | None = None,
) -> tuple[str, str | None]:
    """Pin matching ``packet.ecosystem``. Empty if the only pin is the other eco."""
    from conduit.detect.manifests import flatten_installed, normalize_packet_ecosystem

    pkg = str(packet.get("package") or "")
    eco = str(packet.get("ecosystem") or "")
    by_eco = read_installed_by_ecosystem(root)
    pin = pin_for_packet_ecosystem(by_eco, pkg, eco or None)
    if pin:
        return pin, None
    other = flatten_installed(by_eco).get(pkg.lower()) if pkg else None
    if normalize_packet_ecosystem(eco) and other:
        return "", (
            f"packet ecosystem {eco!r} has no {pkg} pin; "
            f"not binding {other!r} from another ecosystem"
        )
    return str(fallback or "").strip(), None


def _prepare_client_packet(
    root: Path,
    packet: dict,
    *,
    source: dict | None = None,
    installed_version: str | None = None,
    coverage_no_rule: list | None = None,
) -> tuple[dict, dict | None]:
    """Stamp floor from_version from the client pin and prune catalog rules."""
    pkg = str(packet.get("package") or "")
    src = source
    if src is None and pkg:
        src = load_source_packet(root, pkg)
    installed, pin_warning = _ecosystem_client_pin(
        root, packet, fallback=installed_version
    )
    if pin_warning:
        console.print(f"[yellow]Warning:[/yellow] {pin_warning}")
        installed = ""
    if isinstance(src, dict) and installed:
        src = dict(src)
        src["installed_version"] = installed
    floor = str(packet.get("from_version") or "")
    bound = bind_packet_to_client(packet, installed_version=installed)
    if installed and is_snapshot_floor(floor) and not is_snapshot_floor(
        str(bound.get("from_version") or "")
    ):
        console.print(
            f"Bound packet from_version {floor!r} → {bound.get('from_version')!r} "
            f"from client install"
        )
    if coverage_no_rule:
        from conduit.packet.doc_augment import augment_packet_from_docs

        no_rule_payload = [
            {"kind": i.kind, "value": i.value, "detail": i.detail}
            for i in coverage_no_rule
        ]
        bound, aug_warnings = augment_packet_from_docs(
            bound,
            src,
            no_rule_items=no_rule_payload,
            log=console.print,
        )
        for w in aug_warnings:
            if w.startswith("Doc-augmented"):
                console.print(f"[green]{w}[/green]")
            elif w and not w.startswith("evidence"):
                console.print(f"[yellow]Warning:[/yellow] {w}")
    scoped, stats = scope_packet_to_source(bound, src)
    if stats.total:
        console.print(
            f"Pruned to {stats.kept}/{stats.total} packet rules from client usage"
            + (f" (collapsed {stats.collapsed} chain hop(s))" if stats.collapsed else "")
        )
    return scoped, src


def _print_packet_coverage(
    *,
    root: Path,
    package: str,
    detected,
    packet: dict | None = None,
    persist_source: bool = True,
) -> PacketCoverageReport:
    """Print source packet, migration summary, and coverage (will migrate / keep / no rule)."""
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
    if report.no_rule:
        console.print(
            f"[yellow]Coverage:[/yellow] {len(report.no_rule)} client item(s) have "
            "no migrate-from rule (see NO RULE above). KEEP is not a gap."
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
    audit_log=None,
    impact=None,
    docs_synced: list[str] | None = None,
    attempts: int | None = None,
    leftover_lines: list[str] | None = None,
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
        audit_log=audit_log,
        impact=impact,
        docs_synced=docs_synced,
        attempts=attempts,
        leftover_lines=leftover_lines,
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
    audit_log=None,
    impact=None,
    docs_synced: list[str] | None = None,
    attempts: int | None = None,
    leftover_lines: list[str] | None = None,
) -> str:
    """Print the decision-ready run summary. Returns markdown for the PR body."""
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
        audit_log=audit_log,
        impact=impact,
        docs_synced=docs_synced,
        attempts=attempts,
        leftover_lines=leftover_lines,
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
    audit_log=None,
    demo: bool = False,
):
    """Write packet oracle tests, then run the suite with self-correct.

    Shared by ``conduit verify`` and ``conduit run`` so split workflows
    get the same leftover-token checks.
    """
    from conduit.anticheat.audit_log import MigrationAuditLog
    from conduit.llm.client import get_llm_client, resolve_provider

    pkg = str(packet.get("package") or "")
    allowlist = file_allowlist
    if allowlist is None and pkg:
        allowlist = prune_by_imports(root, dependency_packages(packet))

    if audit_log is None:
        audit_log = MigrationAuditLog.from_packet(packet, root=root)

    from conduit.patcher.post_rules.engine import apply_post_rules

    post_report = apply_post_rules(root, packet, file_allowlist=allowlist)
    for change in post_report.changes:
        console.print(f"[post-rule] {change.path}: {change.detail}")

    want_llm = resolve_provider() not in {None, "none", "off", "disabled"}
    beat("hatch")
    try:
        ensure_verify_credentials(
            root,
            packet,
            want_llm=bool(want_llm or get_llm_client()),
            log=console.print,
            console=console,
            demo=demo,
        )
    except CredentialsError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    generated = ensure_tests(
        root,
        packet,
        changed_files=changed_files,
        file_allowlist=allowlist,
        source=source,
    )
    for rel in generated:
        console.print(f"[test-gen] created {rel}")
    if generated:
        audit_log.record_generated(generated)

    edited: list[str] = []
    seen: set[str] = set()
    for rel in list(changed_files or []) + list(generated):
        key = str(rel).replace("\\", "/")
        if key and key not in seen:
            seen.add(key)
            edited.append(key)

    beat("repair")
    result, corrected = verify_with_self_correct(
        root,
        packet,
        max_retries=max_retries,
        verbose=_VERBOSE,
        log=console.print,
        source=source,
        coverage_missed=coverage_missed,
        audit_log=audit_log,
        edited_files=edited,
    )
    try:
        audit_log.persist(root)
    except OSError:
        pass
    return result, generated, corrected, audit_log


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
    *,
    root: Path,
    refresh: bool = False,
    allow_package_name: bool = True,
) -> tuple[Optional[Path], Optional[str]]:
    """
    Interpret --packet as a file path, http(s) URL, or else a package name.
    Returns (packet_file, package_name).
    """
    if not packet:
        return None, None
    raw = packet.strip()
    if not raw:
        return None, None
    if is_packet_url(raw):
        try:
            return fetch_packet_url(raw, root=root, refresh=refresh), None
        except PacketFetchError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
    as_path = Path(raw).expanduser()
    if as_path.is_file():
        return as_path.resolve(), None
    # Bare package names must not look like accidental relative paths with separators
    if any(sep in raw for sep in ("/", "\\")) or raw.endswith(".json"):
        console.print(f"[red]Packet file not found:[/red] {raw}")
        raise typer.Exit(2)
    if not allow_package_name:
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
    packet: str = typer.Option(..., "--packet", help="Path or http(s) URL to conduit-packet.json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Apply a Migration Packet without opening a PR."""
    root = _resolve_root(path)
    from conduit.gitignore import ensure_conduit_gitignore

    gitignore_rel = ensure_conduit_gitignore(root, log=console.print)
    packet_file, _ = _resolve_packet_arg(
        str(packet), root=root, allow_package_name=False
    )
    if packet_file is None:
        console.print("[red]--packet must be a file path or http(s) URL.[/red]")
        raise typer.Exit(2)
    data = json.loads(packet_file.read_text(encoding="utf-8"))
    errors = validate_packet(data)
    if errors:
        for err in errors:
            console.print(f"[red]schema:[/red] {err}")
        raise typer.Exit(1)
    data, _src = _prepare_client_packet(root, data)
    files = prune_by_imports(root, dependency_packages(data))
    sdk_rules, rest_rules, _post, _unknown = partition_rules(list(data.get("rules") or []))
    console.print(f"Applying SDK rules ({len(sdk_rules)})…")
    console.print(f"Applying REST rules ({len(rest_rules)})…")

    from conduit.patcher.impact.engine import analyze_impacts, merge_runtime_packet

    impact = analyze_impacts(root, data, file_allowlist=files or None, log=console.print)
    if impact.blocked:
        console.print(f"[red]Impact analysis blocked migration:[/red] {impact.block_reason}")
        raise typer.Exit(2)

    data = merge_runtime_packet(data, impact.packet_patches)
    if not dry_run:
        from conduit.anticheat.baseline import save_anticheat_baseline

        save_anticheat_baseline(root, files, log=console.print)
    report = apply_packet(
        root,
        data,
        dry_run=dry_run,
        file_allowlist=files or None,
        path_defer=impact.defer_paths,
    )
    if gitignore_rel and gitignore_rel not in report.files_modified:
        report.files_modified.append(gitignore_rel)
    for change in report.changes:
        prefix = "DRY-RUN " if dry_run else ""
        console.print(f"{prefix}[{change.rule_type}] {change.path}: {change.detail}")
    try:
        from conduit.detect.modules.openai import format_model_auto_update_line

        model_line = format_model_auto_update_line(data, report.changes)
        if model_line:
            console.print(model_line)
    except Exception:
        pass
    if not dry_run:
        from conduit.patcher.sync_env import VerifyEnvError, sync_bumped_packages

        try:
            sync_bumped_packages(data, root=root, log=console.print)
        except VerifyEnvError as exc:
            console.print(f"[red]Verify env blocked migration:[/red]\n{exc}")
            raise typer.Exit(2) from exc

        from conduit.patcher.post_rules.engine import apply_post_rules

        post_report = apply_post_rules(
            root,
            data,
            file_allowlist=files or None,
            extra_rules=impact.post_rules,
        )
        for change in post_report.changes:
            console.print(f"[post-rule] {change.path}: {change.detail}")
            if change.path not in report.files_modified:
                report.files_modified.append(change.path)

    console.print(
        f"{'Would modify' if dry_run else 'Modified'} "
        f"{len(report.files_modified)} file(s)."
    )


@app.command("verify")
def verify_cmd(
    path: Path = typer.Option(Path("."), "--path"),
    packet: Optional[str] = typer.Option(
        None, "--packet", help="Path or http(s) URL to conduit-packet.json"
    ),
    max_retries: int = typer.Option(10, "--max-retries"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print self-correct failure/fix details"
    ),
) -> None:
    """Run oracle tests + native suite with optional self-correction."""
    global _VERBOSE
    if verbose:
        _VERBOSE = True
    root = _resolve_root(path)
    from conduit.gitignore import ensure_conduit_gitignore

    ensure_conduit_gitignore(root, log=console.print)
    if packet:
        packet_file, _ = _resolve_packet_arg(
            packet, root=root, allow_package_name=False
        )
        if packet_file is None:
            console.print("[red]--packet must be a file path or http(s) URL.[/red]")
            raise typer.Exit(2)
        data = json.loads(packet_file.read_text(encoding="utf-8"))
    else:
        data = load_fixture_openai_packet()
    data, src = _prepare_client_packet(root, data)
    start_pulse(console, "repair")
    try:
        result, _generated, corrected, _audit = _verify_with_oracle(
            root, data, max_retries=max_retries, source=src
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
        help="Path, http(s) URL, or package name (e.g. openai) for conduit-packet.json",
    ),
    skip_tests: bool = typer.Option(False, "--skip-tests"),
    skip_pr: bool = typer.Option(False, "--skip-pr"),
    no_push: bool = typer.Option(False, "--no-push"),
    skip_modules: bool = typer.Option(False, "--skip-modules"),
    skip_lockfile: bool = typer.Option(False, "--skip-lockfile"),
    skip_export_delta: bool = typer.Option(
        False, "--skip-export-delta", help="Skip package export delta pruning"
    ),
    max_retries: int = typer.Option(10, "--max-retries"),
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
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help="Allow PASSED when high-severity call sites were found but not rewritten",
    ),
) -> None:
    """Full pipeline: detect → prune → packet → apply → verify → PR."""
    global _VERBOSE
    if verbose:
        _VERBOSE = True
    root = _resolve_root(path)
    packet_file, packet_package = _resolve_packet_arg(
        packet, root=root, refresh=refresh_packet
    )
    loaded = load_consumer_env(root)
    preflight_warnings = collect_run_preflight_warnings(
        root,
        packet_file=packet_file,
        demo=demo,
        skip_modules=skip_modules,
        skip_lockfile=skip_lockfile,
        skip_export_delta=skip_export_delta,
        skip_tests=skip_tests,
        skip_pr=skip_pr,
    )
    print_run_preflight(
        console,
        warnings=preflight_warnings,
        env_loaded=loaded,
    )
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
            allow_partial=allow_partial,
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
    allow_partial: bool = False,
) -> None:
    from conduit.gitignore import ensure_conduit_gitignore

    gitignore_rel = ensure_conduit_gitignore(root, log=console.print)
    pkg_hint = package or packet_package
    if package and packet_package and package.lower() != packet_package.lower():
        console.print(
            f"[yellow]Warning:[/yellow] --package {package!r} differs from "
            f"--packet package name {packet_package!r}; using --package"
        )

    published: dict | None = None
    scan_packages = None
    skip_vendor = skip_modules
    if packet_file is not None:
        published = json.loads(packet_file.read_text(encoding="utf-8"))
        file_pkg = str(published.get("package") or "")
        if package and file_pkg and package.lower() != file_pkg.lower():
            console.print(
                f"[yellow]Warning:[/yellow] --package {package!r} differs from "
                f"packet file package {file_pkg!r}; using packet file"
            )
        pkg_hint = file_pkg or pkg_hint
        if not pkg_hint:
            console.print("[red]Packet file has no package field.[/red]")
            raise typer.Exit(2)
        scan_packages = dependency_packages(published)
        skip_vendor = True

    names = [module] if module else _detect_module_names_for_package(pkg_hint)
    beat("detect")
    detected = run_detect(
        root,
        base_ref=base_ref,
        module_names=names,
        skip_modules=skip_vendor,
        skip_lockfile=skip_lockfile,
        scan_client=True,
        scan_packages=scan_packages,
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
        pkt_data = published or json.loads(packet_file.read_text(encoding="utf-8"))
        file_pkg = str(pkt_data.get("package") or "")
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

    pin, _pin_warn = _ecosystem_client_pin(root, pkt)
    state = (detected.package_states or {}).get(pkg) or (
        detected.package_states or {}
    ).get((pkg or "").lower())
    if state is not None:
        state.installed_version = pin or None

    from conduit.packet.bind import bind_packet_to_client

    pkt = bind_packet_to_client(pkt, installed_version=pin)

    beat("prune")
    pkgs = dependency_packages(pkt)
    files = prune_by_imports(root, pkgs)
    console.print(
        f"Pruned to {len(files)} file(s) importing {', '.join(pkgs)}"
    )

    from conduit.export_delta.path_bridge import rules_from_path_bridge
    from conduit.export_delta.usage import (
        collect_package_calls,
        leftover_calls,
        legacy_resource_calls,
        merge_calls_into_api_patterns,
    )
    from conduit.packet.synthesize import merge_packet_rules

    delta = None
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
            if delta.resource_paths:
                _vprint(f"resource paths={len(delta.resource_paths)}")

    calls = collect_package_calls(root, files, pkg)
    if delta is not None and not delta.skipped_reason:
        gone = delta.gone_symbols
        removed_hits = leftover_calls(calls, gone)
        path_hits = legacy_resource_calls(calls, delta.resource_paths)
        if state is not None:
            state.api_patterns = merge_calls_into_api_patterns(
                list(state.api_patterns), [*removed_hits, *path_hits]
            )
        bridge_rules = rules_from_path_bridge(
            resource_paths=delta.resource_paths,
            calls=calls,
        )
        if bridge_rules:
            pkt["rules"] = merge_packet_rules(list(pkt.get("rules") or []), bridge_rules)
            console.print(
                f"Path-bridge: {len(bridge_rules)} AST_CALL_REWRITE rule(s) "
                f"from export delta ∩ {len(path_hits)} resource-path call(s)"
            )

    from conduit.detect.coverage import build_coverage_report

    pre_coverage = build_coverage_report(
        package=pkg,
        state=state,
        signals=detected.signals,
        packet=pkt,
    )
    pkt, src_dict = _prepare_client_packet(
        root,
        pkt,
        source=pre_coverage.source_packet,
        installed_version=pin,
        coverage_no_rule=pre_coverage.no_rule,
    )
    coverage = _print_packet_coverage(
        root=root,
        package=pkg,
        detected=detected,
        packet=pkt,
        persist_source=True,
    )

    beat("apply")
    from conduit.prune.grep_imports import (
        expand_allowlist_for_exact_rules,
        expand_apply_allowlist_oracle,
    )

    before_expand = len(files)
    files = expand_allowlist_for_exact_rules(root, files, pkt)
    files = expand_apply_allowlist_oracle(root, files, pkt, changed_files=None)
    if len(files) != before_expand:
        console.print(
            f"Expanded apply allowlist to {len(files)} file(s) "
            f"(+{len(files) - before_expand} for string-rule hits)"
        )
    sdk_rules, rest_rules, _post, _unknown = partition_rules(list(pkt.get("rules") or []))
    console.print(f"Applying SDK rules ({len(sdk_rules)})…")
    console.print(f"Applying REST rules ({len(rest_rules)})…")

    from conduit.anticheat.audit_log import MigrationAuditLog
    from conduit.patcher.impact.engine import analyze_impacts, merge_runtime_packet

    audit_log = MigrationAuditLog.from_packet(pkt, root=root)
    impact = analyze_impacts(root, pkt, file_allowlist=files or None, log=console.print)
    audit_log.record_impact(impact)
    if impact.blocked:
        console.print(f"[red]Impact analysis blocked migration:[/red] {impact.block_reason}")
        try:
            audit_log.persist(root)
        except OSError:
            pass
        raise typer.Exit(2)

    pkt = merge_runtime_packet(pkt, impact.packet_patches)
    from conduit.anticheat.baseline import save_anticheat_baseline

    save_anticheat_baseline(root, files, log=console.print)
    report = apply_packet(
        root,
        pkt,
        dry_run=False,
        file_allowlist=files or None,
        path_defer=impact.defer_paths,
    )
    if gitignore_rel and gitignore_rel not in report.files_modified:
        report.files_modified.append(gitignore_rel)
    for change in report.changes:
        console.print(f"[{change.rule_type}] {change.path}: {change.detail}")
    try:
        from conduit.detect.modules.openai import format_model_auto_update_line

        model_line = format_model_auto_update_line(pkt, report.changes)
        if model_line:
            console.print(model_line)
    except Exception:
        pass

    from conduit.patcher.sync_env import VerifyEnvError, sync_bumped_packages

    try:
        sync_bumped_packages(pkt, root=root, log=console.print)
    except VerifyEnvError as exc:
        console.print(f"[red]Verify env blocked migration:[/red]\n{exc}")
        try:
            audit_log.persist(root)
        except OSError:
            pass
        raise typer.Exit(2) from exc

    from conduit.patcher.post_rules.engine import apply_post_rules

    post_report = apply_post_rules(
        root,
        pkt,
        file_allowlist=files or None,
        extra_rules=impact.post_rules,
    )
    for change in post_report.changes:
        console.print(f"[post-rule] {change.path}: {change.detail}")
        if change.path not in report.files_modified:
            report.files_modified.append(change.path)

    from conduit.patcher.openai_client_chain import apply_openai_client_chain

    chain_report = apply_openai_client_chain(
        root, files, to_version=str(pkt.get("to_version") or "")
    )
    for change in chain_report.changes:
        console.print(f"[CLIENT_CHAIN] {change.path}: {change.detail}")
        if change.path not in report.files_modified:
            report.files_modified.append(change.path)
    report.merge(chain_report)

    leftover_lines: list[str] = []
    from conduit.patcher.leftovers import (
        format_leftover_lines,
        leftover_handoff_paths,
        leftovers_failure,
        scan_leftovers,
    )

    post_calls = collect_package_calls(root, files, pkg)
    leftover_items = scan_leftovers(
        root=root,
        calls=post_calls,
        delta=delta,
        packet=pkt,
        files=files,
    )
    leftover_lines = [item.display() for item in leftover_items]
    if leftover_items:
        handoff = leftover_handoff_paths(leftover_items)
        if handoff:
            console.print(
                "[yellow]Apply incomplete; handing off to repair: "
                + ", ".join(handoff)
                + "[/yellow]"
            )
        for line in format_leftover_lines(leftover_items):
            console.print(f"[red]{line}[/red]" if not allow_partial else f"[yellow]{line}[/yellow]")
        if not allow_partial:
            from conduit.test_runner import TestResult as _TR

            test_result = leftovers_failure(leftover_items)
            console.print(test_result.summary)
            _print_run_summary(
                packet=pkt,
                report=report,
                test_result=test_result,
                coverage=coverage,
                detected=detected,
                generated=[],
                corrected=[],
                skip_tests=skip_tests,
                audit_log=audit_log,
                impact=impact,
                leftover_lines=leftover_lines,
            )
            raise typer.Exit(2)
        console.print("[yellow]Continuing with --allow-partial despite leftovers.[/yellow]")

    audit_log.record_apply(report)
    try:
        audit_log.persist(root)
    except OSError:
        pass

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
        test_result, generated, corrected, audit_log = _verify_with_oracle(
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
            audit_log=audit_log,
            demo=demo,
        )
        for rel in generated:
            if rel not in report.files_modified:
                report.files_modified.append(rel)
        for rel in corrected:
            console.print(f"[self-correct] updated {rel}")
            if rel not in report.files_modified:
                report.files_modified.append(rel)

    console.print(test_result.summary)
    verify_notes = [
        n
        for n in (getattr(test_result, "extra_notes", None) or [])
        if n.startswith("verify_")
    ]
    if verify_notes:
        console.print("[verify] " + "; ".join(verify_notes))
    docs_synced: list[str] = []
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
            audit_log=audit_log,
            impact=impact,
            leftover_lines=leftover_lines,
        )
        raise typer.Exit(2)

    # Post-green: sync leftover tokens in docs/README/scripts/ops (not mid-repair).
    from conduit.patcher.surface_sync import sync_surfaces

    sync_report = sync_surfaces(root, pkt, log=console.print)
    docs_synced = list(sync_report.files_modified)
    for rel in docs_synced:
        if rel not in report.files_modified:
            report.files_modified.append(rel)
    if docs_synced and audit_log is not None:
        audit_log.record_surface_sync(docs_synced)
        try:
            audit_log.persist(root)
        except OSError:
            pass

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
            audit_log=audit_log,
            impact=impact,
            docs_synced=docs_synced,
            leftover_lines=leftover_lines,
        )
        raise typer.Exit(0)

    detect_summary = "\n".join(
        (
            f"- [{s.source}] {s.package} {s.change_type}: "
            f"{s.description or s.affected_pattern or ''}"
            + (
                f" (shutdown {s.deadline.split('T', 1)[0]})"
                if s.deadline
                else ""
            )
        )
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
            audit_log=audit_log,
            impact=impact,
            docs_synced=docs_synced,
            leftover_lines=leftover_lines,
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
        audit_log=audit_log,
        impact=impact,
        docs_synced=docs_synced,
        leftover_lines=leftover_lines,
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


@packet_app.command("new")
def packet_new_cmd(
    package: Optional[str] = typer.Option(None, "--package", help="Package name"),
    ecosystem: Optional[str] = typer.Option(
        None, "--ecosystem", help="pypi / npm / go / maven (default: pypi)"
    ),
    version: Optional[str] = typer.Option(
        None, "--version", "--to", help="Target version to migrate to"
    ),
    source_url: Optional[List[str]] = typer.Option(
        None,
        "--source-url",
        help="Migration guide / changelog / docs URL (repeatable)",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help="Output JSON path (default packets/{pkg}-{eco}-{version}.json)",
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich", help="Skip LLM enrichment even when configured"
    ),
    scaffold_only: bool = typer.Option(
        False,
        "--scaffold-only",
        help="Write dependency hop + sources only; skip LLM enrichment",
    ),
) -> None:
    """Author a packet from source URLs (TTY prompts or flags)."""
    import sys

    from conduit.packet.author import create_packet_new, default_packet_out_path

    def _ask(label: str, default: str = "") -> str:
        if not sys.stdin.isatty():
            return default
        try:
            return str(typer.prompt(label, default=default or ""))
        except Exception:
            return default

    pkg = (package or "").strip() or _ask("Package")
    if not pkg:
        console.print("[red]--package is required[/red]")
        raise typer.Exit(2)
    eco = (ecosystem or "").strip() or _ask("Ecosystem", "pypi") or "pypi"
    eco = eco.lower()
    ver = (version or "").strip() or _ask("Target version")
    if not ver:
        console.print("[red]--version / --to is required[/red]")
        raise typer.Exit(2)

    urls = [u.strip() for u in (source_url or []) if u and str(u).strip()]
    if sys.stdin.isatty():
        console.print(
            "[dim]Source URLs (migrate guide, changelog, docs). Blank line ends.[/dim]"
        )
        while True:
            raw = _ask("Source URL", "")
            text = (raw or "").strip()
            if not text:
                break
            if text not in urls:
                urls.append(text)

    dest = out or default_packet_out_path(package=pkg, ecosystem=eco, version=ver)
    path_written, packet, warnings = create_packet_new(
        package=pkg,
        ecosystem=eco,
        version=ver,
        source_urls=urls,
        out=dest,
        enrich=not (no_enrich or scaffold_only),
        scaffold_only=scaffold_only,
        log=console.print,
    )
    for warning in warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    n_rules = len(packet.get("rules") or [])
    n_effects = len(packet.get("side_effects") or [])
    console.print(
        f"[green]Wrote[/green] {path_written}  "
        f"({pkg} → {packet.get('to_version')}, {n_rules} rule(s), "
        f"{n_effects} side_effect(s))"
    )
    console.print(
        "[dim]Try it:[/dim] "
        f"conduit packet test --packet {path_written}"
    )
    console.print(
        "[dim]Or:[/dim] "
        f"conduit apply --packet {path_written} --path <consumer> --dry-run"
    )


@packet_app.command("diff-rules")
def packet_diff_rules_cmd(
    packet: Path = typer.Argument(..., help="Current hop packet JSON"),
    previous: Optional[Path] = typer.Option(
        None, "--previous", help="Previous hop packet (else search sibling dir)"
    ),
) -> None:
    """Show rules added/removed vs the previous hop snapshot."""
    from conduit.packet.author import (
        diff_packet_rules,
        load_previous_for_diff,
        summarize_rule,
    )

    current = json.loads(packet.read_text(encoding="utf-8"))
    prev = load_previous_for_diff(
        current,
        previous_path=previous,
        search_dir=packet.parent,
    )
    if prev is None:
        console.print(
            "[yellow]No previous snapshot found "
            "(pass --previous or place an older hop JSON beside this file).[/yellow]"
        )
        raise typer.Exit(1)
    diff = diff_packet_rules(current, prev)
    console.print(
        f"[bold]{current.get('package')}[/bold] "
        f"{prev.get('to_version')} → {current.get('to_version')} "
        f"({current.get('ecosystem')})"
    )
    added = diff["added"]
    removed = diff["removed"]
    console.print(f"[green]Added[/green] ({len(added)})")
    for rule in added:
        console.print(f"  + {summarize_rule(rule)}")
    console.print(f"[red]Removed[/red] ({len(removed)})")
    for rule in removed:
        console.print(f"  - {summarize_rule(rule)}")
    if not added and not removed:
        console.print("[dim]No rule key differences.[/dim]")


@packet_app.command("test")
def packet_test_cmd(
    packet: Path = typer.Option(..., "--packet", help="Migration packet JSON"),
    path: Optional[Path] = typer.Option(
        None,
        "--path",
        help="Optional consumer repo for dry-run apply + coverage",
    ),
) -> None:
    """Validate + summarize a packet; optional dry-run apply (no verify)."""
    from conduit.packet.author import format_packet_summary

    data = json.loads(packet.read_text(encoding="utf-8"))
    errors = validate_packet(data)
    if errors:
        for err in errors:
            console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)

    console.print("[green]Packet is valid.[/green]")
    console.print(format_packet_summary(data))

    if path is None:
        console.print("[dim]packet test OK (validate + summary)[/dim]")
        raise typer.Exit(0)

    from conduit.detect.client_state import scan_package_state

    root = _resolve_root(path)
    report = apply_packet(root, data, dry_run=True, require_context=False)
    console.print(
        f"[dim]Dry-run apply:[/dim] would touch {len(report.files_modified)} file(s), "
        f"{len(report.changes)} change(s)"
    )
    for change in report.changes[:20]:
        console.print(f"  [would] {change.path}: {change.detail}")
    if len(report.changes) > 20:
        console.print(f"  … +{len(report.changes) - 20} more")

    pkg = str(data.get("package") or "")
    try:
        data, _src = _prepare_client_packet(root, data)
    except Exception:
        pass
    state = (
        scan_package_state(root, pkg, demo=False, use_llm=False) if pkg else None
    )
    cov = build_coverage_report(
        package=pkg,
        state=state,
        signals=[],
        packet=data,
    )
    console.print(format_coverage_report(cov, verbose=_VERBOSE))

    effects = data.get("side_effects") or []
    if effects:
        console.print("[bold]Side effects checklist[/bold]")
        for effect in effects:
            if isinstance(effect, dict):
                console.print(
                    f"  • [{effect.get('kind') or 'other'}] {effect.get('detail')}"
                )

    console.print("[green]packet test OK[/green]")


@packet_app.command("export-post-rules")
def packet_export_post_rules_cmd(
    path: Path = typer.Option(Path("."), "--path", help="Consumer repo root"),
    packet_file: Path = typer.Option(..., "--packet", help="Migration packet JSON"),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Output packet path (default: overwrite --packet)"
    ),
    merge: bool = typer.Option(
        True, "--merge/--no-merge", help="Merge learned rules into packet post_rules"
    ),
) -> None:
    """Promote .conduit/post_rules.json into a portable packet."""
    from conduit.patcher.post_rules.store import (
        export_post_rules_to_packet,
        load_learned_post_rules,
    )

    root = _resolve_root(path)
    pkt = json.loads(packet_file.read_text(encoding="utf-8"))
    learned = load_learned_post_rules(root) if merge else []
    if not learned:
        console.print("[yellow]No learned post-rules in .conduit/post_rules.json[/yellow]")
        raise typer.Exit(1)
    merged = export_post_rules_to_packet(pkt, learned)
    dest = out or packet_file
    dest.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    console.print(
        f"[green]Wrote[/green] {len(merged.get('post_rules') or [])} post_rule(s) to {dest}"
    )


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
