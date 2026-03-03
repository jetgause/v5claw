from __future__ import annotations

import asyncio
from typing import Dict, Set

from fastapi import WebSocket


class RunEventHub:
    def __init__(self):
        self._subs: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: str, ws: WebSocket):
        async with self._lock:
            self._subs.setdefault(run_id, set()).add(ws)

    async def unsubscribe(self, run_id: str, ws: WebSocket):
        async with self._lock:
            if run_id in self._subs and ws in self._subs[run_id]:
                self._subs[run_id].remove(ws)
                if not self._subs[run_id]:
                    del self._subs[run_id]

    async def publish(self, run_id: str, msg: dict):
        async with self._lock:
            subs = list(self._subs.get(run_id, set()))
        for ws in subs:
            try:
                await ws.send_json(msg)
            except Exception:
                # best-effort
                pass
