from pathlib import Path

from conduit.patcher.ast_attr_call import apply_call_rewrite, rewrite_python_call


def test_validator_to_field_validator_no_double_prefix():
    src = '@validator("name")\ndef check(cls, v):\n    return v\n'
    out, n = rewrite_python_call(src, "validator", "field_validator")
    assert n >= 1
    assert '@field_validator("name")' in out
    assert "field_field_validator" not in out

    out2, n2 = apply_call_rewrite(
        Path("model.py"), src, old_callee="validator", new_callee="field_validator"
    )
    assert n2 >= 1
    assert '@field_validator("name")' in out2
    assert "field_field_validator" not in out2


def test_non_nested_call_rewrite_still_applies():
    src = "openai.ChatCompletion.create(model='gpt-4')\n"
    out, n = rewrite_python_call(
        src, "openai.ChatCompletion.create", "openai.chat.completions.create"
    )
    assert n >= 1
    assert "openai.chat.completions.create" in out
    assert "ChatCompletion.create" not in out
