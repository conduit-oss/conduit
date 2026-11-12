"""Import merge / dedupe by (module, name). Made-up packages only."""

from __future__ import annotations

import textwrap
from pathlib import Path

from conduit.apply.context import build_context_pack
from conduit.apply.impact_input import sites_from_payload
from conduit.apply.splice import (
    add_imports,
    dedupe_imports,
    parse_model_edit,
    splice_edit,
    strip_inner_imports_already_at_module,
)


def test_add_imports_merges_from_same_module():
    source = "from wirekit import Client\n\ndef f():\n    pass\n"
    out = add_imports(source, ("from wirekit import WireError",))
    assert out.count("from wirekit import") == 1
    assert "Client" in out
    assert "WireError" in out


def test_dedupe_merges_two_from_lines():
    source = textwrap.dedent(
        """\
        from wirekit import Client
        from wirekit import WireError, Client

        def f():
            pass
        """
    )
    out = dedupe_imports(source)
    assert out.count("from wirekit import") == 1
    assert "Client" in out
    assert "WireError" in out
    # Client appears once in the import names.
    import_line = [line for line in out.splitlines() if line.startswith("from wirekit")][0]
    assert import_line.count("Client") == 1


def test_strip_inner_import_already_at_module():
    module = "from wirekit import Client\n"
    fn = textwrap.dedent(
        """\
        def broken(client):
            from wirekit import Client
            return Client()
        """
    )
    out = strip_inner_imports_already_at_module(fn, module, ())
    assert "from wirekit import Client" not in out.split("def", 1)[-1]
    assert "return Client()" in out


def test_splice_merges_new_import_with_existing(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "mod.py").write_text(
        textwrap.dedent(
            """\
            from wirekit import Client

            def broken(client):
                return client.old_call(1)
            """
        ),
        encoding="utf-8",
    )
    payload = {
        "dependency": "wirekit",
        "impacts": [
            {
                "dependency": "wirekit",
                "from_version": "1.0",
                "to_version": "2.0",
                "change": "old_call renamed",
                "kind": "param_renamed",
                "symbol": "wirekit.Client.send",
                "sites": [{"path": "mod.py", "line": 4, "expr": "client.old_call"}],
            }
        ],
    }
    site = sites_from_payload(payload)[0]
    pack = build_context_pack(root, site)
    edit = parse_model_edit(
        '{"function_source": "def broken(client):\\n    return client.send(1)\\n",'
        ' "new_imports": ["from wirekit import WireError"]}'
    )
    patched, _diff = splice_edit(pack, edit)
    assert patched.count("from wirekit import") == 1
    assert "Client" in patched and "WireError" in patched
