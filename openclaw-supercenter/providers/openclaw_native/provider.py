from __future__ import annotations

import os
import asyncio
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any, Dict

app = FastAPI(title="OpenClaw Native Provider (stub)")

PROVIDER_ID = os.environ.get("PROVIDER_ID", "provider.openclaw.native")
PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "OpenClaw Native (stub)")
PORT = int(os.environ.get("PORT", "5005"))
BRIDGE_URL = os.environ.get("BRIDGE_URL")  # e.g. http://supercenter-bridge:8080

CAPABILITIES = [
    # This stub is intentionally broad; real integration would proxy to OpenClaw gateway/CLI.
    "policy.evaluate",
    "notify.send",
    "channel.message.send",
    "audit.query",
]


class InvokeReq(BaseModel):
    run_id: str
    step_id: str
    capability_id: str
    payload: Dict[str, Any] = {}


@app.get("/manifest")
def manifest():
    base_url = os.environ.get("BASE_URL") or f"http://openclaw-native-provider:{PORT}"
    return {
        "provider_id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "base_url": base_url,
        "capabilities": CAPABILITIES,
        "constraints": {"type": "stub"},
    }


@app.get("/health")
def health():
    return {"ok": True, "provider_id": PROVIDER_ID}


@app.post("/invoke")
def invoke(req: InvokeReq):
    # v0: placeholder; actual integration would call OpenClaw gateway/CLI
    return {"ok": True, "provider": PROVIDER_ID, "capability_id": req.capability_id, "echo": req.payload}


async def _auto_register():
    if not BRIDGE_URL:
        return
    m = manifest()
    url = BRIDGE_URL.rstrip("/") + "/providers/register"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(url, json=m)
        except Exception:
            pass


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_auto_register())
