"""ModelPollingWorker: client-used models missing from live /v1/models."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from conduit.detect.client_state import PackageClientState
from conduit.detect.modules.openai.models_legacy import ChangeType, RawSignal, Severity
from conduit.detect.modules.openai.workers.base import Worker, fixtures_dir, resolve_profile

MODELS_URL = "https://api.openai.com/v1/models"


def _model_ids(payload: dict[str, Any]) -> set[str]:
    return {item["id"] for item in payload.get("data", []) if "id" in item}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_live_models(api_key: str, *, url: str = MODELS_URL) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def _client_model_ids(
    client_state: PackageClientState | None,
    *,
    demo: bool,
    fixtures=None,
) -> list[str]:
    if client_state and client_state.model_ids:
        return list(client_state.model_ids)
    if demo:
        used_path = (fixtures or fixtures_dir()) / "models" / "client_used.json"
        if used_path.is_file():
            data = _load_json(used_path)
            if isinstance(data, list):
                return [str(x) for x in data]
            if isinstance(data, dict):
                return [str(x) for x in data.get("model_ids") or []]
    return []


class ModelPollingWorker(Worker):
    name = "ModelPollingWorker"

    def __init__(self) -> None:
        self.last_skip_reason: str | None = None

    def run(
        self,
        *,
        demo: bool = False,
        client_state: PackageClientState | None = None,
        majors_only: bool = True,
        profile=None,
    ) -> list[RawSignal]:
        self.last_skip_reason = None
        prof = resolve_profile(profile)
        vendor = prof.name
        catalog_url = prof.live_catalog_url or MODELS_URL
        auth_env = prof.live_catalog_auth_env or "OPENAI_API_KEY"
        fx = fixtures_dir(prof.fixtures_name)
        used = _client_model_ids(client_state, demo=demo, fixtures=fx)
        if not used:
            self.last_skip_reason = "no_client_models"
            return []

        if demo:
            current = _load_json(fx / "models" / "current_models.json")
        else:
            api_key = os.environ.get(auth_env, "").strip()
            if not api_key:
                self.last_skip_reason = "missing_api_key"
                return []
            current = _fetch_live_models(api_key, url=catalog_url)
            if current is None:
                self.last_skip_reason = "fetch_failed"
                return []

        catalog = _model_ids(current)
        # Case-sensitive catalog ids; compare case-insensitively for client hits
        catalog_lower = {m.lower(): m for m in catalog}
        missing: list[str] = []
        for model_id in used:
            if model_id in catalog or model_id.lower() in catalog_lower:
                continue
            missing.append(model_id)

        signals: list[RawSignal] = []
        for model_id in sorted(set(missing)):
            signals.append(
                RawSignal(
                    vendor=vendor,
                    change_type=ChangeType.MODEL_REMOVED,
                    severity=Severity.CRITICAL,
                    affected_pattern=model_id,
                    replacement_pattern=None,
                    source_url=catalog_url,
                    description=(
                        f"Client uses model {model_id} which is missing from "
                        f"{catalog_url} (replacement from deprecation docs when available)"
                    ),
                )
            )
        return signals
