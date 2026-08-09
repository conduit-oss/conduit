"""Base worker interface for OpenAI signal ingestion."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from conduit.detect.modules.openai.models_legacy import RawSignal


def package_root() -> Path:
    """Installable `conduit/` package root (contains fixtures/)."""
    return Path(__file__).resolve().parents[6]


def fixtures_dir() -> Path:
    candidates = [
        package_root() / "fixtures" / "openai",
        Path(__file__).resolve().parent / "fixtures",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def data_dir() -> Path:
    path = package_root() / "data" / "openai"
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Worker(ABC):
    """Ingestion worker that emits zero or more RawSignal objects."""

    name: str = "worker"

    @abstractmethod
    def run(
        self,
        *,
        demo: bool = False,
        client_state: Any | None = None,
        majors_only: bool = True,
    ) -> list[RawSignal]:
        """Emit signals. Live sources by default; fixtures only when demo=True."""
        raise NotImplementedError
