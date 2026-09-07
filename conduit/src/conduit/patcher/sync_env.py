"""Install bumped packages into the consumer verify interpreter before verify."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def bumped_package_specs(packet: dict[str, Any]) -> list[str]:
    """Return ``package==to_version`` pins for DEPENDENCY_BUMP rules."""
    specs: list[str] = []
    seen: set[str] = set()
    for rule in packet.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "") != "DEPENDENCY_BUMP":
            continue
        eco = str(rule.get("ecosystem") or "").lower()
        ecosystems = rule.get("ecosystems")
        if isinstance(ecosystems, list):
            ecos = {str(e).lower() for e in ecosystems}
        else:
            ecos = {eco} if eco else set()
        # Prefer pip/pyproject bumps; skip pure npm when mixed.
        if ecos and not (ecos & {"pip", "pyproject", "pypi", ""}):
            if ecos <= {"npm", "yarn", "pnpm", "node"}:
                continue
        pkg = str(rule.get("package") or "").strip()
        ver = str(rule.get("to_version") or "").strip().lstrip("=")
        if not pkg or not ver:
            continue
        key = pkg.lower()
        if key in seen:
            continue
        seen.add(key)
        specs.append(f"{pkg}=={ver}")
    return specs


def consumer_req_files(root: Path) -> list[Path]:
    """Prefer root ``requirements.txt``, then shallow pip manifests."""
    root = root.resolve()
    primary = root / "requirements.txt"
    if primary.is_file():
        return [primary]
    try:
        from conduit.detect.pip_manifests import iter_pip_manifests

        found = [
            p
            for p in iter_pip_manifests(root, scope="main")
            if p.name.lower() == "requirements.txt"
        ]
        if found:
            return [found[0]]
    except Exception:
        pass
    return []


def _import_name(spec: str) -> str:
    name = spec.split("==", 1)[0].strip()
    return name.replace("-", "_")


def _run_pip(
    exe: str,
    args: list[str],
    *,
    log=None,
    label: str = "pip install",
    timeout: float = 600,
) -> bool:
    from conduit.pulse import beat

    cmd = [exe, "-m", "pip", *args]
    if log:
        log(f"{label}: {' '.join(args)}")
        log(f"[dim]Into interpreter: {exe}[/dim]")
    beat("wait")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log:
            log(f"[yellow]{label} failed:[/yellow] {exc}")
        return False
    if proc.returncode != 0:
        if log:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = err[-3:] if err else ["(no output)"]
            log(f"[yellow]{label} exit {proc.returncode}:[/yellow] {' '.join(tail)}")
        return False
    return True


def _upgrade_pip(exe: str, *, log=None) -> None:
    _run_pip(
        exe,
        ["install", "--upgrade", "pip"],
        log=log,
        label="pip upgrade",
        timeout=180,
    )


def _pip_install_consumer_reqs(
    exe: str,
    root: Path,
    *,
    force: bool = False,
    log=None,
) -> bool:
    """Install consumer requirements.txt (and optional pyproject) into verify env."""
    reqs = consumer_req_files(root)
    ok = True
    for req in reqs:
        try:
            rel = req.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = req.name
        args = ["install", "--upgrade"]
        if force:
            args.extend(["--force-reinstall", "--no-cache-dir"])
        args.extend(["-r", str(req)])
        if not _run_pip(
            exe,
            args,
            log=log,
            label=f"Installing consumer requirements ({rel})",
            timeout=900,
        ):
            ok = False
    pyproject = root / "pyproject.toml"
    if not reqs and pyproject.is_file():
        args = ["install", "--upgrade"]
        if force:
            args.extend(["--force-reinstall", "--no-cache-dir"])
        args.append(str(root))
        if not _run_pip(
            exe,
            args,
            log=log,
            label="Installing consumer package (pyproject)",
            timeout=900,
        ):
            ok = False
    return ok


def _pip_install_bumps(
    exe: str,
    specs: list[str],
    *,
    force: bool = False,
    log=None,
) -> bool:
    extras = ["pytest"]
    args = ["install", "--upgrade"]
    if force:
        args.extend(["--force-reinstall", "--no-cache-dir"])
    args.extend([*specs, *extras])
    kind = "force-reinstall" if force else "install"
    return _run_pip(
        exe,
        args,
        log=log,
        label=f"Installing bumped packages ({kind}): {' '.join(specs)}",
        timeout=300,
    )


def verify_env_healthy(exe: str, specs: list[str]) -> tuple[bool, str]:
    """True when bumped packages and pytest are importable / runnable."""
    modules = [_import_name(s) for s in specs]
    for mod in modules:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", mod):
            continue
        try:
            proc = subprocess.run(
                [exe, "-c", f"import {mod}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"import {mod}: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = err[-1] if err else f"exit {proc.returncode}"
            return False, f"import {mod}: {detail}"
    try:
        proc = subprocess.run(
            [exe, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pytest: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"exit {proc.returncode}"
        return False, f"pytest: {detail}"
    return True, ""


def _sync_once(
    exe: str,
    specs: list[str],
    *,
    root: Path | None,
    force: bool = False,
    upgrade_pip: bool = False,
    log=None,
) -> bool:
    if upgrade_pip:
        _upgrade_pip(exe, log=log)
    req_ok = True
    if root is not None:
        req_ok = _pip_install_consumer_reqs(exe, root, force=force, log=log)
    bump_ok = True
    if specs:
        bump_ok = _pip_install_bumps(exe, specs, force=force, log=log)
    # Requirements failure is non-fatal if bumps + pytest still healthy.
    return bump_ok if specs else req_ok


def sync_bumped_packages(
    packet: dict[str, Any],
    *,
    python: str | None = None,
    root: Path | None = None,
    log=None,
) -> list[str]:
    """
    Install consumer requirements + DEPENDENCY_BUMP pins + pytest into the
    consumer verify interpreter.

    Order: requirements.txt (or pyproject) first, then bump pins so the
    migrated version wins. Fail-safe: recreate verify-venv and force-reinstall
    on corruption; never aborts the migration for env repair.
    """
    specs = bumped_package_specs(packet)
    has_reqs = bool(root and consumer_req_files(root)) or bool(
        root and (root / "pyproject.toml").is_file()
    )
    if not specs and not has_reqs:
        return []
    if python is None and root is not None:
        from conduit.test_runner import ensure_consumer_python

        python = ensure_consumer_python(root, log=log)
    exe = python or sys.executable
    if root is not None:
        from conduit.test_runner import interpreter_belongs_to_root

        if not interpreter_belongs_to_root(exe, root):
            if log:
                log(
                    f"[yellow]Skipped pip install[/yellow] "
                    f"(interpreter {exe} is not inside {root})"
                )
            return specs

    ok = _sync_once(exe, specs, root=root, force=False, log=log)
    healthy, reason = (
        verify_env_healthy(exe, specs) if (ok and specs) else (ok, "pip install failed")
    )
    if not specs and ok:
        return specs
    if healthy:
        return specs

    if log:
        log(f"[yellow]Verify env unhealthy ({reason}); repairing verify-venv…[/yellow]")

    if root is not None:
        from conduit.test_runner import recreate_verify_venv

        exe = recreate_verify_venv(root, log=log) or exe

    ok = _sync_once(
        exe, specs, root=root, force=True, upgrade_pip=True, log=log
    )
    healthy, reason = (
        verify_env_healthy(exe, specs) if (ok and specs) else (ok, "pip reinstall failed")
    )
    if healthy or (not specs and ok):
        if log:
            log("[green]Verify env repaired.[/green]")
        return specs

    if log:
        log(
            f"[yellow]Verify env still unhealthy after repair ({reason}). "
            "Continuing; tests may be skipped.[/yellow]"
        )
    return specs
