"""Ordinary libraries. Conduit does not track these."""

import pydantic
import requests


def ping(data: dict) -> dict:
    parsed = pydantic.parse_obj_as(dict, data)
    requests.get("https://example.com/health")
    return parsed
