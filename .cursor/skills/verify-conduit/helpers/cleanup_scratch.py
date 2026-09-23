#!/usr/bin/env python3
"""Remove scratch trees created by prove helpers. Never deletes evidence/."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for cand in [here, *here.parents]:
        if (cand / "conduit" / "src" / "conduit").is_dir():
            return cand
    raise SystemExit("could not locate repo root")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Remove every directory under verify-conduit/scratch/",
    )
    parser.add_argument(
        "--path",
        help="Absolute path to one scratch tree to remove",
    )
    args = parser.parse_args()
    skill = find_repo() / ".cursor" / "skills" / "verify-conduit"
    scratch_root = skill / "scratch"
    evidence_root = skill / "evidence"

    if args.path:
        target = Path(args.path).resolve()
        if evidence_root.resolve() in target.parents or target == evidence_root.resolve():
            print("refusing to delete under evidence/", file=sys.stderr)
            return 2
        if not target.exists():
            print(f"missing={target}")
            return 0
        shutil.rmtree(target)
        print(f"removed={target}")
        return 0

    if args.all:
        if not scratch_root.exists():
            print("scratch_root=absent")
            return 0
        for child in scratch_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
                print(f"removed={child}")
        print(f"evidence_kept={evidence_root}")
        return 0

    print("pass --all or --path <scratch>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
