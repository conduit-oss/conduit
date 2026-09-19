"""Watch CLI: pin + leftover old_callee gate."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from conduit.main import app
from conduit.watch import evaluate_watch

REPO = Path(__file__).resolve().parents[2]
SAMPLE_PACKET = REPO / "examples" / "sample-packet" / "conduit-packet.json"

DIRTY_SRC = textwrap.dedent(
    """\
    import openai

    def complete(prompt: str) -> str:
        response = openai.ChatCompletion.create(
            model="gpt-4-0613",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=64,
        )
        return response["choices"][0]["message"]["content"]
    """
)

CLEAN_SRC = textwrap.dedent(
    """\
    from openai import OpenAI

    client = OpenAI()

    def complete(prompt: str) -> str:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=64,
        )
        return response.choices[0].message.content
    """
)


def _packet() -> dict:
    return json.loads(SAMPLE_PACKET.read_text(encoding="utf-8"))


def _make_tree(root: Path, *, pin: str, src: str) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "ai_client.py").write_text(src, encoding="utf-8")
    (root / "requirements.txt").write_text(pin + "\n", encoding="utf-8")
    return root


def test_pre_bump_warns_and_exits_zero(tmp_path: Path):
    tree = _make_tree(tmp_path / "demo", pin="openai==0.28.1", src=DIRTY_SRC)
    verdict = evaluate_watch(root=tree, packet=_packet())
    assert verdict.exit_code == 0
    assert verdict.status == "pre_bump"
    assert verdict.leftovers
    assert any("ChatCompletion" in item.callee for item in verdict.leftovers)
    assert "warn" in verdict.message.lower()


def test_bump_dirty_fails(tmp_path: Path):
    tree = _make_tree(tmp_path / "demo", pin="openai==1.0.0", src=DIRTY_SRC)
    verdict = evaluate_watch(root=tree, packet=_packet())
    assert verdict.exit_code != 0
    assert verdict.status == "bump_dirty"
    assert any("ChatCompletion" in item.callee for item in verdict.leftovers)


def test_clean_pass(tmp_path: Path):
    tree = _make_tree(tmp_path / "demo", pin="openai==1.0.0", src=CLEAN_SRC)
    verdict = evaluate_watch(root=tree, packet=_packet())
    assert verdict.exit_code == 0
    assert verdict.status == "clean"
    assert verdict.leftovers == ()


def test_watch_cli_bump_dirty_exit_and_no_rewrite(tmp_path: Path):
    tree = _make_tree(tmp_path / "demo", pin="openai==1.0.0", src=DIRTY_SRC)
    before = (tree / "src" / "ai_client.py").read_text(encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["watch", "--path", str(tree), "--packet", str(SAMPLE_PACKET)],
    )
    assert result.exit_code != 0
    assert "ChatCompletion" in result.stdout or "leftover" in result.stdout.lower()
    assert (tree / "src" / "ai_client.py").read_text(encoding="utf-8") == before


def test_watch_cli_json_encodes_status(tmp_path: Path):
    tree = _make_tree(tmp_path / "demo", pin="openai==0.28.1", src=DIRTY_SRC)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["watch", "--path", str(tree), "--packet", str(SAMPLE_PACKET), "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "pre_bump"
    assert payload["exit_code"] == 0
    assert payload["leftover_count"] >= 1
