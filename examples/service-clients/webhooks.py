"""Stripe webhook. The newer event schema drops data.object.currency."""

from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    event = await request.json()
    kind = event["type"]
    obj = event["data"]["object"]
    return {"type": kind, "currency": obj["currency"]}
