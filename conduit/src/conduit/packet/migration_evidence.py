"""Build structured migration evidence: docs, code examples, OpenAPI snippets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from conduit.packet.doc_router import resolve_doc_urls
from conduit.packet.evidence import EvidenceDoc, build_evidence, evidence_as_prompt_text

_CODE_FENCE_RE = re.compile(
    r"```(?:\w+)?\n(.*?)```",
    re.DOTALL,
)
_PATH_RE = re.compile(r"/v1/[a-zA-Z0-9_./-]+")
_MAX_EXAMPLES = 6
_MAX_EXAMPLE_CHARS = 2000
_MAX_OPENAPI_PATHS = 8
_MAX_OPENAPI_CHARS = 3500


@dataclass
class MigrationEvidence:
    docs: list[EvidenceDoc] = field(default_factory=list)
    code_examples: list[EvidenceDoc] = field(default_factory=list)
    openapi_structs: list[EvidenceDoc] = field(default_factory=list)
    model_endpoints: list[EvidenceDoc] = field(default_factory=list)
    router_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def preloaded_text(self) -> str:
        parts = [
            evidence_as_prompt_text(self.docs, max_total_chars=28_000),
            evidence_as_prompt_text(self.code_examples, max_total_chars=12_000),
            evidence_as_prompt_text(self.openapi_structs, max_total_chars=12_000),
            evidence_as_prompt_text(self.model_endpoints, max_total_chars=8_000),
        ]
        return "\n".join(p for p in parts if p.strip())

    def as_prompt_dict(
        self,
        *,
        docs_max: int = 20_000,
        examples_max: int = 10_000,
        openapi_max: int = 10_000,
        models_max: int = 6_000,
    ) -> dict[str, str]:
        return {
            "migration_docs": evidence_as_prompt_text(self.docs, max_total_chars=docs_max),
            "code_examples": evidence_as_prompt_text(
                self.code_examples, max_total_chars=examples_max
            ),
            "openapi_structs": evidence_as_prompt_text(
                self.openapi_structs, max_total_chars=openapi_max
            ),
            "model_endpoints": evidence_as_prompt_text(
                self.model_endpoints, max_total_chars=models_max
            ),
        }


def extract_code_examples(
    docs: Iterable[EvidenceDoc],
    *,
    max_examples: int = _MAX_EXAMPLES,
    max_chars: int = _MAX_EXAMPLE_CHARS,
) -> list[EvidenceDoc]:
    out: list[EvidenceDoc] = []
    for doc in docs:
        for idx, block in enumerate(_CODE_FENCE_RE.findall(doc.text or "")):
            text = block.strip()
            if len(text) < 20:
                continue
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…"
            out.append(
                EvidenceDoc(
                    url=f"{doc.url}#example-{idx + 1}",
                    title=f"example from {doc.title or doc.url}",
                    text=text,
                    kind="example",
                )
            )
            if len(out) >= max_examples:
                return out
    return out


def _request_body_summary(path_item: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for method, op in path_item.items():
        if method.startswith("x-") or not isinstance(op, dict):
            continue
        entry: dict[str, Any] = {
            "summary": op.get("summary"),
            "deprecated": op.get("deprecated"),
        }
        body = (op.get("requestBody") or {}).get("content") or {}
        schemas: dict[str, Any] = {}
        for media, spec in body.items():
            schema = (spec or {}).get("schema") or {}
            props = (schema.get("properties") or {})
            required = schema.get("required") or []
            if props or required:
                schemas[str(media)] = {
                    "properties": list(props.keys()),
                    "required": list(required),
                }
        if schemas:
            entry["requestBody"] = schemas
        summary[method.upper()] = entry
    return summary


def extract_openapi_snippets(
    *,
    paths: Iterable[str],
    demo: bool = True,
    profile=None,
) -> list[EvidenceDoc]:
    from conduit.detect.modules.openai.workers.openapi_diff import load_openapi_pair

    pair = load_openapi_pair(demo=demo, profile=profile)
    if not pair:
        return []
    _previous, latest = pair
    spec_paths = (latest.get("paths") or {})
    out: list[EvidenceDoc] = []
    seen: set[str] = set()
    for raw in paths:
        path = str(raw or "").strip()
        if not path.startswith("/v1/") or path in seen:
            continue
        seen.add(path)
        item = spec_paths.get(path)
        if not isinstance(item, dict):
            continue
        snippet = {path: _request_body_summary(item)}
        text = json.dumps(snippet, indent=2)
        if len(text) > _MAX_OPENAPI_CHARS:
            text = text[:_MAX_OPENAPI_CHARS] + "\n…"
        out.append(
            EvidenceDoc(
                url=f"openapi:{path}",
                title=f"OpenAPI {path}",
                text=text,
                kind="openapi",
            )
        )
        if len(out) >= _MAX_OPENAPI_PATHS:
            break
    return out


def _collect_context_paths(context_chunks: Iterable[str]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for chunk in context_chunks:
        for match in _PATH_RE.findall(str(chunk or "")):
            low = match.lower()
            if low not in seen:
                seen.add(low)
                paths.append(match)
    return paths


def _model_endpoint_excerpt(doc: EvidenceDoc) -> EvidenceDoc | None:
    text = doc.text or ""
    if "supported endpoints" not in text.lower() and "endpoint" not in text.lower():
        return None
    lines: list[str] = []
    capture = False
    for line in text.splitlines():
        low = line.lower()
        if "supported endpoints" in low or low.startswith("## endpoints"):
            capture = True
        if capture:
            lines.append(line)
            if len(lines) > 40:
                break
    if not lines:
        return None
    return EvidenceDoc(
        url=doc.url,
        title=doc.title or doc.url,
        text="\n".join(lines)[:4000],
        kind="model",
    )


def build_migration_evidence(
    *,
    context_chunks: Iterable[str],
    profile,
    search_queries: list[str] | None = None,
    model_ids: Iterable[str] | None = None,
    max_pages: int = 8,
    open_search: bool = False,
    demo_openapi: bool = True,
) -> MigrationEvidence:
    """Fetch and structure migration evidence for detect enrich or repair."""
    chunks = [str(c) for c in context_chunks if c]
    seeds = list(getattr(profile, "evidence_seeds", None) or [])
    hosts = list(getattr(profile, "evidence_hosts", None) or ["github.com"])

    router_urls = resolve_doc_urls(
        context_chunks=chunks,
        profile_seeds=seeds,
        max_urls=max_pages,
    )
    for mid in list(model_ids or [])[:6]:
        url_fn = getattr(profile, "model_doc_url", None)
        if callable(url_fn):
            url = url_fn(str(mid))
            if url and url not in router_urls:
                router_urls.append(url)

    merged_seeds: list[str] = []
    seen: set[str] = set()
    for url in router_urls + seeds:
        if url and url not in seen:
            seen.add(url)
            merged_seeds.append(url)

    docs, warnings = build_evidence(
        seed_urls=merged_seeds,
        allow_hosts=hosts,
        search_queries=search_queries,
        max_seed_pages=max_pages,
        open_search=open_search,
    )

    examples = extract_code_examples(docs)
    paths = _collect_context_paths(chunks)
    for doc in docs:
        paths.extend(_collect_context_paths([doc.text]))
    openapi = extract_openapi_snippets(
        paths=sorted(set(paths)),
        demo=demo_openapi,
        profile=profile,
    )

    model_docs: list[EvidenceDoc] = []
    for doc in docs:
        if doc.kind in {"seed", "link"} and "/models/" in doc.url:
            excerpt = _model_endpoint_excerpt(doc)
            if excerpt:
                model_docs.append(excerpt)

    return MigrationEvidence(
        docs=docs,
        code_examples=examples,
        openapi_structs=openapi,
        model_endpoints=model_docs,
        router_urls=router_urls,
        warnings=list(warnings),
    )
