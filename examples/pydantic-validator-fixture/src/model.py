"""Tiny pydantic v1 fixture for the Watch-visible hop smoke."""

from __future__ import annotations

from pydantic import BaseModel, validator


class User(BaseModel):
    name: str

    @validator("name")
    def check_name(cls, v):
        return v

    class Config:
        orm_mode = True

    def dump(self):
        return self.dict()
