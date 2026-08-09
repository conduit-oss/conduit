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
from conduit.pr_generator import open_pull_request
from conduit.prune.grep_imports import prune_by_imports
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
) -> None:
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


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print extra diagnostics"
    ),
) -> None:
    global _VERBOSE
    _VERBOSE = verbose


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
    if "openai" in packages:
        return "openai"
    return packages[0] if packages else None


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
    result = run_detect(
        root,
        base_ref=base_ref,
        majors_only=majors_only,
        module_names=names,
        skip_modules=skip_modules,
        skip_lockfile=skip_lockfile,
        demo=demo,
        verbose=_VERBOSE,
    )
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

            pkg0 = "openai" if "openai" in pkgs else pkgs[0]
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
        pkg0 = "openai" if "openai" in {p.lower() for p in pkgs} else pkgs[0]
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
    files = prune_by_imports(root, [data.get("package", "")])
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
    """Run native tests with optional self-correction."""
    global _VERBOSE
    if verbose:
        _VERBOSE = True
    root = _resolve_root(path)
    data = (
        json.loads(packet.read_text(encoding="utf-8"))
        if packet
        else load_fixture_openai_packet()
    )
    result, corrected = verify_with_self_correct(
        root, data, max_retries=max_retries, verbose=_VERBOSE, log=console.print
    )
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

    pkg_hint = package or packet_package
    if package and packet_package and package.lower() != packet_package.lower():
        console.print(
            f"[yellow]Warning:[/yellow] --package {package!r} differs from "
            f"--packet package name {packet_package!r}; using --package"
        )

    names = [module] if module else _detect_module_names_for_package(pkg_hint)
    if demo:
        console.print("[dim]Demo mode: using offline detect fixtures[/dim]")
    detected = run_detect(
        root,
        base_ref=base_ref,
        module_names=names,
        skip_modules=skip_modules,
        skip_lockfile=skip_lockfile,
        demo=demo,
        verbose=_VERBOSE,
    )
    for warning in detected.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")
    if _VERBOSE:
        by_type: dict[str, int] = {}
        for s in detected.signals:
            by_type[s.change_type] = by_type.get(s.change_type, 0) + 1
        _vprint(
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
        ensured = ensure_packet(
            root,
            detected.signals,
            package=pkg,
            packet_path=packet_file,
            installed=detected.installed,
            use_fixture_fallback=demo,
            refresh=refresh_packet,
        )
    else:
        if pkg is None:
            if any(s.package == "openai" for s in detected.signals):
                pkg = "openai"
            else:
                console.print("[green]No migration signals found. Nothing to do.[/green]")
                raise typer.Exit(0)
        ensured = ensure_packet(
            root,
            detected.signals,
            package=pkg,
            packet_path=None,
            installed=detected.installed,
            use_fixture_fallback=demo,
            refresh=refresh_packet,
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
    _print_packet_coverage(
        root=root,
        package=pkg,
        detected=detected,
        packet=pkt,
        persist_source=True,
    )

    files = prune_by_imports(root, [pkg])
    console.print(f"Pruned to {len(files)} file(s) importing {pkg}")

    if not skip_export_delta:
        from_v = str(pkt.get("from_version") or "")
        to_v = str(pkt.get("to_version") or "")
        eco = str(pkt.get("ecosystem") or "pypi")
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
        corrected: list[str] = []
    else:
        generated = ensure_tests(root, pkt, changed_files=report.files_modified)
        for rel in generated:
            console.print(f"[test-gen] created {rel}")
            if rel not in report.files_modified:
                report.files_modified.append(rel)

        test_result, corrected = verify_with_self_correct(
            root, pkt, max_retries=max_retries, verbose=_VERBOSE, log=console.print
        )
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
        raise typer.Exit(2)

    if skip_pr:
        console.print("[green]Patches applied and tests passed (PR skipped).[/green]")
        raise typer.Exit(0)

    summary = "\n".join(
        f"- [{s.source}] {s.package} {s.change_type}: "
        f"{s.description or s.affected_pattern or ''}"
        for s in detected.signals[:20]
    )
    pr = open_pull_request(
        root,
        pkt,
        report,
        test_result,
        push=not no_push,
        create_pr=True,
        detect_summary=summary,
    )
    console.print(pr.message)
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
) -> None:
    """Scaffold a new detect module."""
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
    )
    console.print(f"[green]Created module stub at[/green] {mod_dir}")
    console.print("Next: implement run(), then `conduit module list`.")


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


if __name__ == "__main__":
    app()
