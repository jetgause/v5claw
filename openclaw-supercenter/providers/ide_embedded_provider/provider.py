from __future__ import annotations

import os
import json
import subprocess
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel

PORT = int(os.environ.get("PORT", "5011"))
PROVIDER_ID = os.environ.get("PROVIDER_ID", "provider.ide_embedded")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8080")
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "/app/data/workspaces")

app = FastAPI(title=PROVIDER_ID)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class InvokeRequest(BaseModel):
    capability_id: str
    payload: Dict[str, Any] = {}
    run_id: Optional[str] = None
    step_id: Optional[str] = None

def safe_join(root: str, rel: str) -> str:
    rel = rel.lstrip("/").replace("\\", "/")
    path = os.path.abspath(os.path.join(root, rel))
    if not path.startswith(os.path.abspath(root) + os.sep) and path != os.path.abspath(root):
        raise HTTPException(status_code=400, detail="Path escape not allowed")
    return path

def list_tree(base: str, max_entries: int = 5000) -> List[Dict[str, Any]]:
    out = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        for d in dirnames:
            if count >= max_entries:
                return out
            out.append({"type":"dir","path": os.path.normpath(os.path.join(rel_dir, d)).replace("\\","/")})
            count += 1
        for f in filenames:
            if count >= max_entries:
                return out
            fp = os.path.join(dirpath, f)
            try:
                size = os.path.getsize(fp)
            except Exception:
                size = None
            out.append({"type":"file","path": os.path.normpath(os.path.join(rel_dir, f)).replace("\\","/"), "size": size})
            count += 1
    return out

def parse_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """Very small unified diff parser. Returns list of file patches with hunks.
    Supports headers: --- a/.. +++ b/.. and hunks @@ -l,s +l,s @@.
    """
    lines = diff_text.splitlines()
    i = 0
    patches = []
    current = None
    while i < len(lines):
        line = lines[i]
        if line.startswith('--- '):
            old = line[4:].strip()
            i += 1
            if i >= len(lines) or not lines[i].startswith('+++ '):
                raise ValueError("Malformed diff: missing +++")
            new = lines[i][4:].strip()
            # strip a/ b/
            def strip_prefix(p):
                if p.startswith('a/') or p.startswith('b/'):
                    return p[2:]
                return p
            path = strip_prefix(new)
            current = {"path": path, "hunks": []}
            patches.append(current)
            i += 1
            continue
        if line.startswith('@@ ') and current is not None:
            m = re.match(r'^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@', line)
            if not m:
                raise ValueError("Malformed hunk header")
            old_start = int(m.group(1))
            old_len = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_len = int(m.group(4) or "1")
            i += 1
            hunk_lines = []
            while i < len(lines) and not lines[i].startswith('@@ ') and not lines[i].startswith('--- '):
                hunk_lines.append(lines[i])
                i += 1
            current["hunks"].append({
                "old_start": old_start, "old_len": old_len,
                "new_start": new_start, "new_len": new_len,
                "lines": hunk_lines
            })
            continue
        i += 1
    return patches

import re

def apply_unified_diff_to_text(original: str, file_patch: Dict[str, Any]) -> str:
    """Apply hunks sequentially based on new_start/old_start. This is simplified and assumes context matches."""
    src_lines = originalinal.splitlines()
    # keep as list without line endings; join with \n
    offset = 0
    for h in file_patch["hunks"]:
        # old_start is 1-based
        idx = h["old_start"] - 1 + offset
        # Build new chunk
        new_chunk = []
        src_i = idx
        for l in h["lines"]:
            if l.startswith(' '):
                # context
                if src_i >= len(src_lines) or src_lines[src_i] != l[1:]:
                    # best effort: don't hard fail; try to continue
                    pass
                new_chunk.append(l[1:])
                src_i += 1
            elif l.startswith('-'):
                src_i += 1
            elif l.startswith('+'):
                new_chunk.append(l[1:])
            elif l.startswith('\\'):
                # '\ No newline at end of file' - ignore
                pass
            else:
                # treat as context
                new_chunk.append(l)
        # Replace the affected old_len lines starting at idx with new_chunk
        # old_len may not equal consumed; use h['old_len']
        del src_lines[idx: idx + h["old_len"]]
        src_lines[idx:idx] = new_chunk
        offset += (len(new_chunk) - h["old_len"])
    return "\n".join(src_lines) + ("\n" if original.endswith("\n") else "")

@app.get("/manifest")
def manifest():
    return {
        "provider_id": PROVIDER_ID,
        "title": "Embedded IDE Workspace Provider",
        "capabilities": [
            "workspace.fs.list",
            "workspace.fs.read",
            "workspace.fs.write",
            "ide.apply_diff",
            "workspace.exec.command",
        ],
        "constraints": {"requires_workspace_root": True},
        "ts": now_iso(),
    }

@app.post("/invoke")
async def invoke(req: InvokeRequest = Body(...)):
    cap = req.capability_id
    payload = req.payload or {}
    if cap == "workspace.fs.list":
        rel = payload.get("path","")
        base = safe_join(WORKSPACE_ROOT, rel)
        if not os.path.exists(base):
            raise HTTPException(status_code=404, detail="Path not found")
        if os.path.isfile(base):
            return {"ok": True, "items": [{"type":"file","path": rel, "size": os.path.getsize(base)}]}
        return {"ok": True, "items": list_tree(base)}
    if cap == "workspace.fs.read":
        rel = payload.get("path")
        if not rel:
            raise HTTPException(status_code=400, detail="payload.path required")
        fp = safe_join(WORKSPACE_ROOT, rel)
        if not os.path.exists(fp) or not os.path.isfile(fp):
            raise HTTPException(status_code=404, detail="File not found")
        with open(fp, "r", encoding=payload.get("encoding","utf-8"), errors="replace") as f:
            txt = f.read()
        return {"ok": True, "text": txt}
    if cap == "workspace.fs.write":
        rel = payload.get("path")
        if not rel:
            raise HTTPException(status_code=400, detail="payload.path required")
        fp = safe_join(WORKSPACE_ROOT, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding=payload.get("encoding","utf-8")) as f:
            f.write(payload.get("text",""))
        return {"ok": True}
    if cap == "ide.apply_diff":
        diff_text = payload.get("diff")
        if not diff_text:
            raise HTTPException(status_code=400, detail="payload.diff required")
        patches = parse_unified_diff(diff_text)
        applied = []
        for p in patches:
            fp = safe_join(WORKSPACE_ROOT, p["path"])
            if not os.path.exists(fp):
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write("")
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                orig = f.read()
            new_txt = apply_unified_diff_to_text(orig, p)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(new_txt)
            applied.append(p["path"])
        return {"ok": True, "ap
if cap == "workspace.exec.command":
        cmd = payload.get("command")
        if not cmd:
            raise HTTPException(status_code=400, detail="payload.command required")
        cwd = safe_join(WORKSPACE_ROOT, payload.get("cwd",""))
        timeout_s = int(payload.get("timeout", 600))
        stream = bool(payload.get("stream", True))

        async def publish_log(kind: str, text: str):
            if not (req.run_id and req.step_id):
                return
            try:
                token = os.environ.get("PROVIDER_TOKEN", "")
                headers = {"X-Provider-Token": token} if token else {}
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"{BRIDGE_URL}/runs/{req.run_id}/events/publish", headers=headers, json={
                        "ts": now_iso(),
                        "run_id": req.run_id,
                        "step_id": req.step_id,
                        "type": kind,
                        "provider_id": PROVIDER_ID,
                        "payload": {"text": text, "command": cmd, "cwd": payload.get("cwd","")},
                    })
            except Exception:
                pass

        if not stream:
            try:
                proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
                return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout[-20000:], "stderr": proc.stderr[-20000:]}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "timeout"}

        proc = await asyncio.create_subprocess_shell(cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def reader(stream, etype):
            buf = []
            while True:
                line = await stream.readline()
                if not line:
                    break
                txt = line.decode(errors="replace")
                buf.append(txt)
                await publish_log(etype, txt)
            return "".join(buf)

        out_task = asyncio.create_task(reader(proc.stdout, "log.stdout"))
        err_task = asyncio.create_task(reader(proc.stderr, "log.stderr"))
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await publish_log("log.stderr", "\n[timeout]\n")
            return {"ok": False, "error": "timeout"}
        stdout = await out_task
        stderr = await err_task
        return {"ok": rc == 0, "returncode": rc, "stdout": stdout[-20000:], "stderr": stderr[-20000:]}
, "error": "timeout"}

    return {"ok": False, "error": f"Capability not supported: {cap}"}

@app.on_event("startup")
async def _register():
    # auto-register with bridge
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            base_url = os.environ.get("BASE_URL", f"http://ide-embedded-provider:{PORT}")
            # If running locally, BASE_URL can be set externally.
            await client.post(f"{BRIDGE_URL}/providers/register", json={
                "provider_id": PROVIDER_ID,
                "base_url": base_url,
                "manifest": manifest(),
            })
    except Exception:
        pass
