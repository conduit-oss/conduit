"""Put the package root on ``sys.path`` so ``from tests.apply_support`` works.

CI runs ``python -m pytest conduit/tests`` from the monorepo root. Without this,
``tests`` is not importable because the package root is ``conduit/``, not the
checkout root.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_root = str(_ROOT)
if _root not in sys.path:
    sys.path.insert(0, _root)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "network: tests that call the public internet",
    )
