from src.gemini_client import complete


def test_importable():
    assert callable(complete)
