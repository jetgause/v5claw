from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel


"""Agent Zero Adapter Provider (best-effort wiring).

This provider does **not** modify Agent Zero. It provides a minimal bridge:
  - health/manifest
  - generic HTTP proxy capability for calling Agent Zero endpoints if you know them
  - convenience capability to return the Agent Zero UI URL

Agent Zero public docs emphasize extensible API endpoints, but the exact
endpoint set can vary by version/config. So we wire a safe proxy surface that
lets OpenClaw Super Center drive Agent Zero without hard-coding unstable paths.
"""


app = FastAPI(title="Agent Zero Adapter Provider")

PROVIDER_ID = os.environ.get("PROVIDER_ID", "provider.agentzero")
PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "Agent Zero Adapter")
PORT = int(os.environ.get("PORT", "5007"))

BRIDGE_URL = os.environ.get("BRIDGE_URL")  # e.g. http://supercenter-bridge:8080
AGENTZERO_BASE_URL = os.environ.get("AGENTZERO_BASE_URL", "http://agentzero:80")


CAPABILITIES = [
    "agentzero.ui.url",
    "agentzero.http",  # generic proxy
]


class InvokeReq(BaseModel):
    run_id: str
    step_id: str
    capability_id: str
    payload: Dict[str, Any] = {}


@app.get("/manifest")
def manifest():
    base_url = os.environ.get("BASE_URL") or f"http://agentzero-adapter:{PORT}"
    return {
        "provider_id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "capabilities": CAPABILITIES,
        "constraints": {"upstream": "agent-zero", "type": "adapter"},
    }


@app.get("/health")
def health():
    return {"ok": True, "provider_id": PROVIDER_ID, "agentzero_base_url": AGENTZERO_BASE_URL}


@app.post("/invoke")
async def invoke(req: InvokeReq):
    cap = req.capability_id
    if cap == "agentzero.ui.url":
        return {"ok": True, "url": AGENTZERO_BASE_URL.rstrip("/") + "/"}

    if cap == "agentzero.http":
        # payload: {"method": "GET"|"POST"|..., "path": "/api/...", "json": {...}, "params": {...}}
        method = (req.payload.get("method") or "GET").upper()
        path = req.payload.get("path") or "/"
        params = req.payload.get("params")
        j = req.payload.get("json")
        data = req.payload.get("data")
        url = AGENTZERO_BASE_URL.rstrip("/") + path
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(method, url, params=params, json=j, data=data)
            # return as text to avoid schema surprises
            return {
                "ok": r.is_success,
                "status_code": r.status_code,
                "headers": dict(r.headers),
                "text": r.text,
            }

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
