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


def _import_name(spec: str) -> str:
    name = spec.split("==", 1)[0].strip()
    return name.replace("-", "_")


def _pip_install(
    exe: str,
    specs: list[str],
    *,
    force: bool = False,
    upgrade_pip: bool = False,
    log=None,
) -> bool:
    from conduit.pulse import beat

    if upgrade_pip:
        beat("wait")
        try:
            proc = subprocess.run(
                [exe, "-m", "pip", "install", "--upgrade", "pip"],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if log:
                log(f"[yellow]pip upgrade skipped:[/yellow] {exc}")
        else:
            if proc.returncode != 0 and log:
                err = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = err[-2:] if err else ["(no output)"]
                log(f"[yellow]pip upgrade exit {proc.returncode}:[/yellow] {' '.join(tail)}")

    extras = ["pytest"]
    cmd = [exe, "-m", "pip", "install", "--upgrade"]
    if force:
        cmd.extend(["--force-reinstall", "--no-cache-dir"])
    cmd.extend([*specs, *extras])
    if log:
        joined = " ".join(specs)
        kind = "force-reinstall" if force else "install"
        log(f"Installing bumped packages ({kind}): {joined}")
        log(f"[dim]Into interpreter: {exe}[/dim]")
    beat("wait")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log:
            log(f"[yellow]pip install failed:[/yellow] {exc}")
        return False
    if proc.returncode != 0:
        if log:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = err[-3:] if err else ["(no output)"]
            log(f"[yellow]pip install exit {proc.returncode}:[/yellow] {' '.join(tail)}")
        return False
    return True


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


def sync_bumped_packages(
    packet: dict[str, Any],
    *,
    python: str | None = None,
    root: Path | None = None,
    log=None,
) -> list[str]:
    """
    Install DEPENDENCY_BUMP pins + pytest into the consumer verify interpreter.

    Fail-safe: on pip/corruption, recreate ``.conduit/verify-venv``, force
    reinstall, and smoke-check. Never aborts the migration for env repair.
    """
    specs = bumped_package_specs(packet)
    if not specs:
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

    ok = _pip_install(exe, specs, force=False, log=log)
    healthy, reason = verify_env_healthy(exe, specs) if ok else (False, "pip install failed")
    if healthy:
        return specs

    if log:
        log(f"[yellow]Verify env unhealthy ({reason}); repairing verify-venv…[/yellow]")

    if root is not None:
        from conduit.test_runner import recreate_verify_venv

        exe = recreate_verify_venv(root, log=log) or exe

    ok = _pip_install(exe, specs, force=True, upgrade_pip=True, log=log)
    healthy, reason = verify_env_healthy(exe, specs) if ok else (False, "pip reinstall failed")
    if healthy:
        if log:
            log("[green]Verify env repaired.[/green]")
        return specs

    if log:
        log(
            f"[yellow]Verify env still unhealthy after repair ({reason}). "
            "Continuing; tests may be skipped.[/yellow]"
        )
    return specs
