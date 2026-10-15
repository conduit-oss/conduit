"""Call the deferred apply pipeline without a ``conduit apply`` command."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import typer

from conduit.main import apply_migration, console


def invoke_apply(path: Path | str, packet: Path | str, *, dry_run: bool = False) -> SimpleNamespace:
    buf = StringIO()
    # ``console.file`` falls through to the live stdout when ``_file`` is unset.
    # Saving that live object and writing it back pins the console, so later
    # CliRunner swaps of sys.stdout are ignored. Restore the private field.
    previous = console._file
    console.file = buf
    code = 0
    try:
        apply_migration(Path(path), str(packet), dry_run=dry_run)
    except typer.Exit as exc:
        code = int(exc.exit_code or 0)
    finally:
        console._file = previous
    text = buf.getvalue()
    return SimpleNamespace(exit_code=code, output=text, stdout=text, stderr="")
