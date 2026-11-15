"""Decode dependency manifests and lockfiles that are not plain UTF-8.

Windows tools (PowerShell ``pip freeze > requirements.txt``) and editors often
write UTF-16 with a BOM, UTF-8 with a BOM, or legacy 8-bit bytes (cp1252 /
latin-1). Callers must use :func:`read_manifest_text` instead of
``Path.read_text(encoding="utf-8")`` so one unreadable file cannot abort impact.
"""

from __future__ import annotations

import warnings
from pathlib import Path

# Byte-order marks. ``utf-16`` consumes the BOM and picks endianness.
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe", "utf-16"),  # UTF-16 LE
    (b"\xfe\xff", "utf-16"),  # UTF-16 BE
    (b"\xef\xbb\xbf", "utf-8-sig"),
)


def decode_manifest_bytes(data: bytes) -> str:
    """Decode manifest/lock bytes. Raises ``UnicodeDecodeError`` when undecodable."""
    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            return data.decode(encoding)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Sensible 8-bit fallback (cp1252 / latin-1). Reject NULs: those are not
    # typical requirements/lock text in an 8-bit encoding (often mis-tagged
    # UTF-16 or binary), so treat them as undecodable rather than mojibake.
    if b"\x00" in data:
        raise UnicodeDecodeError(
            "manifest",
            data,
            0,
            max(1, data.find(b"\x00") + 1),
            "binary or unsupported encoding",
        )
    return data.decode("cp1252")


def read_manifest_text(path: Path) -> str | None:
    """Return decoded manifest/lock text, or ``None`` when unreadable/undecodable.

    Decode failures emit a :class:`UserWarning` so callers can skip the file
    without looking like a clean “no pin found” miss. ``OSError`` (missing
    file, permission) returns ``None`` without a warning, matching prior
    silent ``except OSError: continue`` behavior at call sites.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return decode_manifest_bytes(data)
    except UnicodeDecodeError as exc:
        warnings.warn(
            f"could not decode dependency manifest {path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
        return None
