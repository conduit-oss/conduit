"""Re-export the legacy helper and the ChatCompletion symbol."""

from openai import ChatCompletion

from .impl import complete_chat

__all__ = ["ChatCompletion", "complete_chat"]
