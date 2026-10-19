"""Put the package root on ``sys.path`` so ``from tests.apply_support`` works.

CI runs ``python -m pytest conduit/tests`` from the monorepo root. Without this,
``tests`` is not importable because the package root is ``conduit/``, not the
checkout root.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_root = str(_ROOT)
if _root not in sys.path:
    sys.path.insert(0, _root)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "network: tests that call the public internet",
    )
    config.addinivalue_line(
        "markers",
        "nebius: tests that call Nebius Token Factory (need NEBIUS_API_KEY)",
    )


@pytest.fixture(scope="session", autouse=True)
def _conduit_analysis_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Isolate analysis cache per suite; disable disk writes in unit tests.

    Large wheels (SQLAlchemy) produce multi‑10k symbol dumps that are too heavy
    to serialize on every cold impact in CI. Dedicated cache tests opt in.
    """
    root = tmp_path_factory.mktemp("conduit-cache")
    os.environ["CONDUIT_CACHE_DIR"] = str(root)
    os.environ["CONDUIT_NO_CACHE"] = "1"
    try:
        from conduit.analysis_cache import analysis_source_digest

        analysis_source_digest.cache_clear()
    except Exception:
        pass
    yield
