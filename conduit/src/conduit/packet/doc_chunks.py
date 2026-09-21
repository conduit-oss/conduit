"""Turn fetched HTML/docs into plain-text chunks for mint prompts."""

from __future__ import annotations

import re
from typing import Iterable


def html_to_plain_text(html: str) -> str:
    """Best-effort strip of tags/scripts for LLM context (not a full browser)."""
    text = html or ""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]\s*>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_plain_text(
    text: str,
    *,
    max_chars: int = 10000,
    max_chunks: int = 6,
) -> list[str]:
    """
    Split plain text into bounded chunks for multi-window mint prompts.

    Prefers paragraph boundaries; falls back to hard slices.
    """
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= max_chars:
        return [body]

    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        candidate = f"{buf}\n\n{para}".strip() if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            if len(chunks) >= max_chunks:
                return chunks
        if len(para) <= max_chars:
            buf = para
        else:
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
                if len(chunks) >= max_chunks:
                    return chunks
            buf = ""
    if buf and len(chunks) < max_chunks:
        chunks.append(buf)
    return chunks[:max_chunks]


def migration_docs_payload(
    pages: Iterable[tuple[str, str]],
    *,
    max_chars: int = 10000,
    max_chunks: int = 6,
) -> list[dict[str, str]]:
    """
    Build ``migration_docs`` list entries ``{url, text}`` from fetched bodies.

    ``pages`` is ``(url, raw_html_or_text)``.
    """
    out: list[dict[str, str]] = []
    for url, raw in pages:
        plain = html_to_plain_text(raw)
        for i, chunk in enumerate(
            chunk_plain_text(plain, max_chars=max_chars, max_chunks=max_chunks)
        ):
            label = url if i == 0 else f"{url}#chunk-{i + 1}"
            out.append({"url": label, "text": chunk})
        if len(out) >= max_chunks:
            break
    return out[:max_chunks]
