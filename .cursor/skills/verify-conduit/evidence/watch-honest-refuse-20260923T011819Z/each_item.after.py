"""Declared synthetic: each_item=True — permanent refuse; Watch stays dirty."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class User(BaseModel):
    tags: list[str]

    @field_validator("tags", each_item=True)
    @classmethod
    def check_tags(cls, v):
        return v
