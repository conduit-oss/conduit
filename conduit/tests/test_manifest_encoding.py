"""Dependency manifests are not always UTF-8; decode them instead of crashing."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from conduit.deps.manifests import read_installed
from conduit.deps.text import decode_manifest_bytes, read_manifest_text
from conduit.impact.run import NO_PIN_NOTE
from conduit.impact.versions import resolve_client_version
from conduit.outside_in.distributions import declared_distribution_names

_PIN = "acmekit==1.2.0\n"
_APP = (
    "import acmekit\n"
    "\n"
    "\n"
    "def ask(prompt: str) -> str:\n"
    '    reply = acmekit.Thing.create(model="acme-small", '
    'messages=[{"role": "user", "content": prompt}])\n'
    "    return reply.choices[0].message.content\n"
)


def _repo_with_requirements(root: Path, raw: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "requirements.txt").write_bytes(raw)
    (root / "app.py").write_text(_APP, encoding="utf-8")
    return root


def _utf16_be_bom(text: str) -> bytes:
    return b"\xfe\xff" + text.encode("utf-16-be")


@pytest.mark.parametrize(
    "label,raw",
    [
        ("utf-16-le-bom", _PIN.encode("utf-16")),
        ("utf-16-be-bom", _utf16_be_bom(_PIN)),
        ("utf-8-bom", _PIN.encode("utf-8-sig")),
        ("latin-1", "acmekit==1.2.0  # café\n".encode("latin-1")),
        ("utf-8-plain", _PIN.encode("utf-8")),
    ],
)
def test_encoded_requirements_find_acmekit_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, raw: bytes
):
    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "")
    root = _repo_with_requirements(tmp_path / label, raw)

    assert read_manifest_text(root / "requirements.txt") is not None
    assert "acmekit" in read_installed(root)
    assert read_installed(root)["acmekit"] == "1.2.0"
    names = {name.lower() for name in declared_distribution_names(root)}
    assert "acmekit" in names
    version, reason = resolve_client_version(root, "acmekit", allow_network=False)
    assert version == "1.2.0"
    assert reason == ""


def test_plain_utf8_requirements_still_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "")
    root = _repo_with_requirements(tmp_path / "plain", _PIN.encode("utf-8"))
    assert (root / "requirements.txt").read_bytes()[:3] != b"\xef\xbb\xbf"
    assert read_installed(root)["acmekit"] == "1.2.0"
    assert resolve_client_version(root, "acmekit", allow_network=False) == ("1.2.0", "")


def test_undecodable_manifest_warns_and_is_not_clean_no_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("conduit.impact.versions.installed_version", lambda _package: "")
    # NULs + a non-UTF-8 high byte: not UTF-8, not BOM UTF-16, not sensible 8-bit text.
    raw = b"\x00\xff\x00\xfe\x00\x01\x02"
    root = _repo_with_requirements(tmp_path / "bad", raw)

    with pytest.warns(UserWarning, match="could not decode dependency manifest"):
        assert read_manifest_text(root / "requirements.txt") is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        installed = read_installed(root)
        names = declared_distribution_names(root)
        version, reason = resolve_client_version(root, "acmekit", allow_network=False)

    assert installed == {}
    assert "acmekit" not in {n.lower() for n in names}
    assert version == ""
    assert reason != NO_PIN_NOTE
    assert "could not decode dependency manifest" in reason
    assert "requirements.txt" in reason
    assert any("could not decode dependency manifest" in str(item.message) for item in caught)


def test_decode_manifest_bytes_rejects_nul_heavy_8bit():
    with pytest.raises(UnicodeDecodeError):
        decode_manifest_bytes(b"\x00\xff\x00\xfe")
    assert decode_manifest_bytes(_PIN.encode("utf-8")) == _PIN
    assert "acmekit" in decode_manifest_bytes(_PIN.encode("utf-16"))
    assert "café" in decode_manifest_bytes("acmekit==1.2.0  # café\n".encode("latin-1"))
