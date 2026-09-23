#!/usr/bin/env python3
"""Copy a consumer tree into an isolated scratch dir (never mutate examples in place)."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


IGNORE = shutil.ignore_patterns(
    ".pytest_cache",
    "__pycache__",
    ".conduit",
    ".venv",
    "venv",
    ".git",
    "*.pyc",
)


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "conduit" / "src" / "conduit").is_dir():
            return cand
    raise SystemExit("could not locate repo root")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="Repo-relative or absolute path to consumer tree",
    )
    parser.add_argument(
        "--dest",
        required=True,
        help="Absolute path for disposable copy (created/replaced)",
    )
    args = parser.parse_args()
    repo = find_repo()
    source = Path(args.source)
    if not source.is_absolute():
        source = repo / source
    if not source.is_dir():
        print(f"error: source not a directory: {source}", file=sys.stderr)
        return 1
    dest = Path(args.dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=IGNORE)
    print(f"isolated={dest}")
    print(f"source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
