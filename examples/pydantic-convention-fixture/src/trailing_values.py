"""Declared synthetic: trailing values → info.data."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class Order(BaseModel):
    total: int
    currency: str = "USD"

    @field_validator("total")
    @classmethod
    def check_total(cls, v, values):
        return values["currency"]
