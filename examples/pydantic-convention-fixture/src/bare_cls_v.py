"""Declared synthetic: bare (cls, v) — already valid v2; apply is a no-op."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class User(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        return v
