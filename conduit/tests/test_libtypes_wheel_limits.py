"""Libtypes must not unpack classify sdists or oversized wheels."""

from __future__ import annotations

from pathlib import Path

from conduit.outside_in import library_types as lt


def test_unpack_and_pick_url_reject_sdist_and_oversized_wheel(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(lt, "_MAX_DOWNLOAD", 64)
    monkeypatch.setattr(lt, "_libtypes_cache", lambda: tmp_path / "cache")

    sdist = tmp_path / "demo-1.0.tar.gz"
    sdist.write_bytes(b"sdist-body")
    assert lt._unpack(sdist, "demo") is None

    oversized = tmp_path / "demo-1.0-py3-none-any.whl"
    oversized.write_bytes(b"x" * 128)
    assert lt._unpack(oversized, "demo") is None

    assert (
        lt._pick_url(
            [
                {
                    "filename": "demo-1.0.tar.gz",
                    "url": "https://example.test/demo-1.0.tar.gz",
                    "size": 10,
                    "packagetype": "sdist",
                }
            ]
        )
        is None
    )

    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir()
    (wheels_dir / "demo-1.0.tar.gz").write_bytes(b"sdist")
    (wheels_dir / "demo-1.0-py3-none-any.whl").write_bytes(b"y" * 128)
    monkeypatch.setattr(lt, "_wheel_dirs", lambda: (wheels_dir,))
    assert lt._cached_wheel("demo", "1.0") is None
