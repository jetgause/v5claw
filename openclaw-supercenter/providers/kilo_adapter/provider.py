from __future__ import annotations

import os
import asyncio
from typing import Any, Dict

import httpx
from fastapi import FastAPI
from pydantic import BaseModel


"""Kilo Adapter Provider (best-effort wiring).

Kilo is primarily a VS Code extension and a CLI. There is also a Kilo AI Gateway
API in the docs. Because deployments vary, this adapter exposes:
  - kilo.ui.url (optional)
  - kilo.http (generic proxy to a configured base URL)

You can point KILO_BASE_URL at:
  - a self-hosted Kilo Gateway
  - a local wrapper you run
  - any compatible endpoint you want to treat as "Kilo backend"
"""


app = FastAPI(title="Kilo Adapter Provider")

PROVIDER_ID = os.environ.get("PROVIDER_ID", "provider.kilo")
PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "Kilo Adapter")
PORT = int(os.environ.get("PORT", "5008"))

BRIDGE_URL = os.environ.get("BRIDGE_URL")
KILO_BASE_URL = os.environ.get("KILO_BASE_URL", "http://localhost:0")  # set to real endpoint


CAPABILITIES = [
    "kilo.ui.url",
    "kilo.http",
]


class InvokeReq(BaseModel):
    run_id: str
    step_id: str
    capability_id: str
    payload: Dict[str, Any] = {}


@app.get("/manifest")
def manifest():
    base_url = os.environ.get("BASE_URL") or f"http://kilo-adapter:{PORT}"
    return {
        "provider_id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "capabilities": CAPABILITIES,
        "constraints": {"upstream": "kilocode", "type": "adapter"},
    }


@app.get("/health")
def health():
    return {"ok": True, "provider_id": PROVIDER_ID, "kilo_base_url": KILO_BASE_URL}


@app.post("/invoke")
async def invoke(req: InvokeReq):
    cap = req.capability_id
    if cap == "kilo.ui.url":
        return {"ok": True, "url": KILO_BASE_URL}

    if cap == "kilo.http":
        method = (req.payload.get("method") or "GET").upper()
        path = req.payload.get("path") or "/"
        params = req.payload.get("params")
        j = req.payload.get("json")
        data = req.payload.get("data")
        url = KILO_BASE_URL.rstrip("/") + path
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(method, url, params=params, json=j, data=data)
            return {"ok": r.is_success, "status_code": r.status_code, "headers": dict(r.headers), "text": r.text}

    return {"ok": False, "error": f"Unsupported capability: {cap}", "payload": req.payload}


async def _auto_register():
    if not BRIDGE_URL:
        return
    url = BRIDGE_URL.rstrip("/") + "/providers/register"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, json=manifest())
        except Exception:
            pass


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_auto_register())
