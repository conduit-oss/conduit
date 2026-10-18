"""Webhook chains require a payload from the outside service."""

from __future__ import annotations

import ast
import textwrap

from conduit.outside_in.graph import ModUnit
from conduit.outside_in.specs import ServiceSpec
from conduit.outside_in.webhooks import webhook_directs

_SPEC = ServiceSpec(service_id="ledgerdb", package="ledgerdb", modules=("ledgerdb",))


def _directs(source: str) -> list:
    unit = ModUnit(rel="routes/player_route.py", tree=ast.parse(textwrap.dedent(source)))
    return webhook_directs({"routes/player_route.py": unit}, (_SPEC,))


def test_route_that_mentions_a_service_is_not_a_webhook():
    """A CRUD route is not a webhook because the file imports the service.

    The handler docstring names the service and the route sets an X- response
    header. Neither is evidence that an outside service posted a payload.
    """
    found = _directs(
        """
        from ledgerdb.ext.asyncio import AsyncSession
        from fastapi import APIRouter, Depends, Response

        api_router = APIRouter()

        @api_router.get(
            "/players/",
            summary="Retrieves a collection of players",
        )
        async def get_all_async(response: Response, async_session: AsyncSession):
            \"\"\"async_session: The async version of a ledgerdb ORM session.\"\"\"
            response.headers["X-Cache"] = "HIT"
            if response.headers["X-Cache"] == "HIT":
                response.headers["X-Cache"] = "MISS"
            return []
        """
    )
    assert found == []


def test_docstring_signature_word_does_not_bind_a_service():
    found = _directs(
        """
        from fastapi import APIRouter, Response

        api_router = APIRouter()

        @api_router.post("/players/")
        async def post_async(response: Response):
            \"\"\"Verify the call signature, then store a ledgerdb session.\"\"\"
            response.headers["X-Request-Id"] = "1"
            return {}
        """
    )
    assert found == []


def test_event_type_on_a_named_webhook_route_is_a_webhook():
    found = _directs(
        """
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.post("/ledgerdb/webhook")
        async def intake(request: Request):
            event = await request.json()
            kind = event["type"]
            obj = event["data"]["object"]
            return {"type": kind, "currency": obj["currency"]}
        """
    )
    assert found
    assert {item.service for item in found} == {"ledgerdb"}
    assert any(item.response_fields == ("data.object.currency",) for item in found)


def test_signature_header_names_the_service():
    found = _directs(
        """
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.post("/hooks")
        async def intake(request: Request):
            sig = request.headers.get("Ledgerdb-Signature")
            if not sig:
                return None
            return sig
        """
    )
    assert len(found) == 1
    assert found[0].service == "ledgerdb"
    assert found[0].response_fields == ("Ledgerdb-Signature",)


def test_event_type_without_the_service_on_the_route_is_not_that_service():
    found = _directs(
        """
        from fastapi import FastAPI, Request

        app = FastAPI()

        @app.post("/webhook")
        async def intake(request: Request):
            \"\"\"Receives ledgerdb events.\"\"\"
            event = await request.json()
            return event["type"]
        """
    )
    assert found == []
