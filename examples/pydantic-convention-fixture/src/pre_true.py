"""Declared synthetic: literal pre=True → mode='before'."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class User(BaseModel):
    name: str

    @field_validator("name", pre=True)
    @classmethod
    def check_name(cls, v):
        return v
