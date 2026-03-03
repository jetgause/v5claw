from __future__ import annotations

import os
import asyncio
from typing import Any, Dict

import httpx
from fastapi import FastAPI
from pydantic import BaseModel


"""Roo Adapter Provider (best-effort wiring).

Roo is primarily a VS Code extension. This adapter is meant for future remote
Roo deployments (or cloud wrappers). For now it provides:
  - roo.ui.url (optional)
  - roo.http (generic proxy)

If you run Roo in a remote agent wrapper that exposes REST endpoints (common
pattern in the community), point ROO_BASE_URL at it.
"""


app = FastAPI(title="Roo Adapter Provider")

PROVIDER_ID = os.environ.get("PROVIDER_ID", "provider.roo")
PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "Roo Adapter")
PORT = int(os.environ.get("PORT", "5009"))

BRIDGE_URL = os.environ.get("BRIDGE_URL")
ROO_BASE_URL = os.environ.get("ROO_BASE_URL", "http://localhost:0")


CAPABILITIES = [
    "roo.ui.url",
    "roo.http",
]


class InvokeReq(BaseModel):
    run_id: str
    step_id: str
    capability_id: str
    payload: Dict[str, Any] = {}


@app.get("/manifest")
def manifest():
    base_url = os.environ.get("BASE_URL") or f"http://roo-adapter:{PORT}"
    return {
        "provider_id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "capabilities": CAPABILITIES,
        "constraints": {"upstream": "roo-code", "type": "adapter"},
    }


@app.get("/health")
def health():
    return {"ok": True, "provider_id": PROVIDER_ID, "roo_base_url": ROO_BASE_URL}


@app.post("/invoke")
async def invoke(req: InvokeReq):
    cap = req.capability_id
    if cap == "roo.ui.url":
        return {"ok": True, "url": ROO_BASE_URL}

    if cap == "roo.http":
        method = (req.payload.get("method") or "GET").upper()
        path = req.payload.get("path") or "/"
        params = req.payload.get("params")
        j = req.payload.get("json")
        data = req.payload.get("data")
        url = ROO_BASE_URL.rstrip("/") + path
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
