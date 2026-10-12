"""A local create() that must not be treated as an OpenAI call."""

from __future__ import annotations


class Store:
    def create(self, row: dict) -> dict:
        return row


def save(row: dict) -> dict:
    return Store().create(row)
