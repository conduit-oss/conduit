from ai_client import DEFAULT_MODEL, complete


def test_default_model_is_legacy():
    # Intentionally asserts the legacy id so packet REST rules can update it.
    assert DEFAULT_MODEL == "gpt-4-0613"


def test_complete_uses_legacy_chatcompletion(monkeypatch):
    calls = {}

    def fake_create(**kwargs):
        calls.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}}],
        }

    # Join segments so packet string leftovers do not rewrite this patch path.
    target = ".".join(("openai", "ChatCompletion", "create"))
    monkeypatch.setattr(target, fake_create)
    assert complete("hi") == "ok"
    assert calls["model"] == "gpt-4-0613"
    assert "max_tokens" in calls
