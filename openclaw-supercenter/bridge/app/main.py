
from __future__ import annotations

import os
import json
import base64
import asyncio
import httpx
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .models.schemas import (
    ProviderManifest, RunCreate, RunStepCreate, Event, ArtifactMeta, ProjectCreate,
    AssetPut, ApprovalCreate, ApprovalRespond, SecretCreate, AutomationPut
)
from .services.store import JsonStore, RunRecord, new_id, now_iso
from .services.ws import RunEventHub


DATA_DIR = os.environ.get("SUPER_CENTER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
PROVIDER_TOKEN = os.environ.get("SUPER_CENTER_PROVIDER_TOKEN", "")

app = FastAPI(title="OpenClaw SuperCenter Bridge", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

store = JsonStore(DATA_DIR)
hub = RunEventHub()



from fastapi import Header
from fastapi.responses import FileResponse
import tempfile
import zipfile as _zipfile


def _sha256_bytes(b: bytes) -> str:
    import hashlib as _hashlib
    h = _hashlib.sha256()
    h.update(b)
    return h.hexdigest()

def _persist_auto_artifacts(run_id: str, provider_id: str | None, step_id: str | None, result: dict):
    """Persist common artifacts returned inline by providers.
    Supported keys:
      - screenshot_base64 / image_base64 (png)
      - pdf_base64 (pdf)
      - text (txt)
      - trace_json (json)
    Returns list of artifact meta dicts.
    """
    out = []
    try:
        art_root = os.path.join(store.data_dir, "artifacts", run_id)
        os.makedirs(art_root, exist_ok=True)

        def write_blob(name: str, data: bytes):
            fn = f"{new_id('art')}_{name}"
            path = os.path.join(art_root, fn)
            with open(path, "wb") as f:
                f.write(data)
            meta = {"filename": fn, "path": path, "sha256": _sha256_bytes(data), "size": len(data), "provider_id": provider_id, "step_id": step_id}
            out.append(meta)
            return meta

        if isinstance(result, dict):
            if result.get("screenshot_base64") or result.get("image_base64"):
                b64 = result.get("screenshot_base64") or result.get("image_base64")
                try:
                    data = base64.b64decode(b64)
                    write_blob("screenshot.png", data)
                except Exception:
                    pass
            if result.get("pdf_base64"):
                try:
                    data = base64.b64decode(result["pdf_base64"])
                    write_blob("page.pdf", data)
                except Exception:
                    pass
            if result.get("trace_json"):
                try:
                    data = json.dumps(result["trace_json"], indent=2).encode("utf-8")
                    write_blob("trace.json", data)
                except Exception:
                    pass
            if result.get("text"):
                try:
                    data = str(result["text"]).encode("utf-8")
                    write_blob("output.txt", data)
                except Exception:
                    pass
    except Exception:
        return out
    return out
def _check_provider_token(x_provider_token: str | None):
    if PROVIDER_TOKEN:
        if not x_provider_token or x_provider_token != PROVIDER_TOKEN:
            raise HTTPException(401, "invalid provider token")
def emit(event: Event):
    store.append_event(event)


async def emit_and_publish(event: Event):
    emit(event)
    await hub.publish(event.run_id, event.model_dump())


def get_access_profile(name: str) -> Dict[str, Any]:
    # v0 profiles: simple and intentionally explicit.
    profiles = {
        "standard": {
            "auto_approve": False,
            "requires_approval_cap_prefixes": ["workspace.exec.", "ide.run_command"],
        },
        "elevated": {
            "auto_approve": True,
            "requires_approval_cap_prefixes": [],  # still allow operator to configure later
        },
    }
    return profiles.get(name, profiles["standard"])


def capability_requires_approval(capability_id: str, access_profile: str) -> bool:
    p = get_access_profile(access_profile)
    if p.get("auto_approve"):
        return False
    for pref in p.get("requires_approval_cap_prefixes", []):
        if capability_id.startswith(pref):
            return True
    return False


def pick_provider_for_capability(capability_id: str) -> Optional[ProviderManifest]:
    # naive: first provider that claims capability
    for rec in store.providers.values():
        if capability_id in rec.manifest.capabilities:
            return rec.manifest
    return None


async def invoke_provider(manifest: ProviderManifest, run_id: str, step_id: str, capability_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = manifest.base_url.rstrip("/") + "/invoke"
    body = {"run_id": run_id, "step_id": step_id, "capability_id": capability_id, "payload": payload}
    timeout = float(manifest.constraints.get("timeout_s", 120.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json()


@app.get("/health")
def health():
    return {"ok": True, "ts": now_iso(), "version": app.version}


# ---------------- Providers ----------------
@app.post("/providers/register")
def register_provider(manifest: ProviderManifest):
    store.add_provider(manifest)
    return {"ok": True}


@app.get("/providers")
def list_providers():
    out = []
    for pid, rec in store.providers.items():
        out.append({**rec.manifest.model_dump(), "last_seen": rec.last_seen})
    return out


# ---------------- Projects ----------------
@app.post("/projects")
def create_project(req: ProjectCreate):
    proj = req.model_dump()
    proj_id = store.add_project(proj)
    return {"project_id": proj_id, **store.projects[proj_id]}


@app.get("/projects")
def list_projects():
    return list(store.projects.values())


@app.get("/projects/{project_id}")
def get_project(project_id: str):
    if project_id not in store.projects:
        raise HTTPException(404, "project not found")
    return store.projects[project_id]


# ---------------- Assets (Modes/Skills/Tools/Agents) ----------------
@app.get("/assets/{kind}")
def list_assets(kind: str):
    if kind not in store.assets:
        raise HTTPException(400, "invalid asset kind")
    return store.assets[kind]


@app.put("/assets/{kind}")
def put_asset(kind: str, req: AssetPut):
    if kind not in store.assets:
        raise HTTPException(400, "invalid asset kind")
    store.asset_put(kind, req.key, req.value)
    return {"ok": True, "kind": kind, "key": req.key}


@app.delete("/assets/{kind}/{key}")
def delete_asset(kind: str, key: str):
    if kind not in store.assets:
        raise HTTPException(400, "invalid asset kind")
    store.asset_delete(kind, key)
    return {"ok": True}


# ---------------- Secrets (handles) ----------------
@app.post("/secrets")
def create_secret(req: SecretCreate):
    # WARNING: v0 stores plain text. Replace with Vault/KMS.
    rec = {"name": req.name, "value": req.value, "meta": req.meta, "created_at": now_iso(), "bound_projects": []}
    hid = store.secret_create(rec)
    return {"handle_id": hid, **store.secrets[hid]}


@app.get("/secrets")
def list_secrets():
    # do not return values by default
    out = []
    for hid, rec in store.secrets.items():
        out.append({"handle_id": hid, "name": rec.get("name"), "meta": rec.get("meta", {}), "created_at": rec.get("created_at"), "bound_projects": rec.get("bound_projects", [])})
    return out


@app.post("/secrets/{handle_id}/bind/{project_id}")
def bind_secret(handle_id: str, project_id: str):
    if handle_id not in store.secrets:
        raise HTTPException(404, "secret handle not found")
    if project_id not in store.projects:
        raise HTTPException(404, "project not found")
    bp = store.secrets[handle_id].get("bound_projects", [])
    if project_id not in bp:
        bp.append(project_id)
    store.secret_update(handle_id, {"bound_projects": bp})
    return {"ok": True}


# ---------------- Approvals ----------------
@app.post("/approvals")
async def create_approval(req: ApprovalCreate):
    record = req.model_dump()
    record.update({"status": "pending", "created_at": now_iso()})
    aid = store.approval_create(record)
    await emit_and_publish(Event(ts=now_iso(), run_id=req.run_id, step_id=req.step_id, type="policy.prompt", payload={"approval_id": aid, **record}))
    return {"approval_id": aid, **store.approvals[aid]}


@app.get("/approvals")
def list_approvals(status: Optional[str] = None):
    out = list(store.approvals.values())
    if status:
        out = [a for a in out if a.get("status") == status]
    return out


@app.post("/approvals/{approval_id}/respond")
async def respond_approval(approval_id: str, req: ApprovalRespond):
    if approval_id not in store.approvals:
        raise HTTPException(404, "approval not found")
    rec = store.approvals[approval_id]
    if rec.get("status") != "pending":
        return {"ok": True, "status": rec.get("status")}
    decision = req.decision.lower()
    if decision not in ("approved", "denied"):
        raise HTTPException(400, "decision must be approved or denied")

    store.approval_update(approval_id, {"status": decision, "decided_at": now_iso(), "note": req.note})
    await emit_and_publish(Event(ts=now_iso(), run_id=rec["run_id"], step_id=rec.get("step_id"), type="policy.decision", payload={"approval_id": approval_id, "decision": decision, "note": req.note}))

    # If approved, resume step execution
    if decision == "approved" and rec.get("run_id") and rec.get("step_id"):
        await execute_step(rec["run_id"], rec["step_id"], force=True)
    return {"ok": True, "status": decision}


# ---------------- Automation (basic CRUD only) ----------------
@app.get("/automation/{kind}")
def list_automation(kind: str):
    if kind not in store.automation:
        raise HTTPException(400, "invalid automation kind")
    return store.automation[kind]


@app.put("/automation/{kind}")
def put_automation(kind: str, req: AutomationPut):
    if kind not in store.automation:
        raise HTTPException(400, "invalid automation kind")
    store.automation_put(kind, req.key, req.value)
    return {"ok": True}


@app.delete("/automation/{kind}/{key}")
def delete_automation(kind: str, key: str):
    if kind not in store.automation:
        raise HTTPException(400, "invalid automation kind")
    store.automation_delete(kind, key)
    return {"ok": True}


# ---------------- Runs ----------------
@app.post("/runs")
async def create_run(req: RunCreate):
    run_id = new_id("run")
    title = req.title or "Untitled Run"
    run = RunRecord(run_id=run_id, project_id=req.project_id, title=title, prompt=req.prompt, access_profile=req.access_profile)
    store.add_run(run)
    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, type="run.started", payload={"title": title, "project_id": req.project_id, "prompt": req.prompt, "access_profile": req.access_profile}))
    return {"run_id": run_id, **run.__dict__}


@app.get("/runs")
def list_runs():
    return [r.__dict__ for r in store.runs.values()]


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    if run_id not in store.runs:
        raise HTTPException(404, "run not found")
    r = store.runs[run_id]
    return {"run": r.__dict__, "events": [e.model_dump() for e in store.events_by_run.get(run_id, [])]}


@app.post("/runs/{run_id}/steps")
async def add_step(run_id: str, req: RunStepCreate):
    if run_id not in store.runs:
        raise HTTPException(404, "run not found")

    step_id = new_id("step")
    run = store.runs[run_id]

    step = {
        "step_id": step_id,
        "capability_id": req.capability_id,
        "payload": req.payload,
        "provider_id": req.provider_id,
        "targets": req.targets,
        "status": "planned",
        "created_at": now_iso(),
    }
    run.steps.append(step)
    store.update_run(run_id)

    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.planned", payload={"capability_id": req.capability_id, "provider_id": req.provider_id, "targets": req.targets}))

    # Execute immediately for v0.2, but route through approvals if needed.
    await execute_step(run_id, step_id)

    return {"step_id": step_id}


async def execute_step(run_id: str, step_id: str, force: bool = False):
    run = store.runs[run_id]
    step = next((s for s in run.steps if s["step_id"] == step_id), None)
    if not step:
        return

    cap = step["capability_id"]
    access_profile = run.access_profile or "standard"

    # If step needs approval, pause and enqueue.
    if not force and capability_requires_approval(cap, access_profile):
        step["status"] = "waiting_approval"
        store.update_run(run_id)
        await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.waiting_approval", payload={"capability_id": cap, "access_profile": access_profile}))
        await create_approval(ApprovalCreate(run_id=run_id, step_id=step_id, capability_id=cap, reason=f"Access profile '{access_profile}' requires approval for '{cap}'", payload=step.get("payload") or {}))
        return

    # Determine provider(s)
    provider_ids: List[str] = []
    if step.get("provider_id"):
        provider_ids = [step["provider_id"]]
    elif step.get("targets"):
        provider_ids = step["targets"]

    manifests: List[ProviderManifest] = []
    if provider_ids:
        for pid in provider_ids:
            if pid in store.providers:
                manifests.append(store.providers[pid].manifest)
    else:
        m = pick_provider_for_capability(cap)
        if m:
            manifests = [m]

    step["status"] = "running"
    store.update_run(run_id)
    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.started", provider_id=manifests[0].provider_id if manifests else None, payload={"capability_id": cap}))

    if not manifests:
        step["status"] = "failed"
        store.update_run(run_id)
        await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.failed", payload={"error": "no provider available", "capability_id": cap}))
        return

    # Execute providers in parallel (maxed)
    async def call_one(m: ProviderManifest):
        try:
            res = await invoke_provider(m, run_id, step_id, cap, step.get("payload") or {})
            return {"provider_id": m.provider_id, "result": res}
        except Exception as e:
            return {"provider_id": m.provider_id, "error": str(e)}

    
results = await asyncio.gather(*[call_one(m) for m in manifests])

    # Emit progress per provider and persist common inline artifacts
    auto_artifacts = []
    for r in results:
        pid = r.get("provider_id")
        if "result" in r:
            await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.progress", provider_id=pid, payload={"message": "provider completed", "result": r["result"]}))
            # auto persist artifacts returned inline
            try:
                metas = _persist_auto_artifacts(run_id, pid, step_id, r["result"])
                for mta in metas:
                    auto_artifacts.append(mta)
                    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="artifact.created", provider_id=pid, payload={"type":"auto", **mta}))
            except Exception:
                pass
        else:
            await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.progress", provider_id=pid, payload={"message": "provider error", "error": r.get("error")}))

    # Strategy selection:
    # - all: all providers must succeed (default)
    # - race: first successful result wins; failures tolerated
    # - vote: mark success if any succeed; include all results for UI/consensus
    strategy = (step.get("payload") or {}).get("_strategy") or (step.get("payload") or {}).get("strategy") or "all"
    ok_results = [r for r in results if "result" in r]
    any_ok = len(ok_results) > 0
    all_ok = all("result" in r for r in results)

    winner = None
    if strategy == "race" and any_ok:
        winner = ok_results[0]
    elif strategy in ("vote","any") and any_ok:
        # winner is first ok, but keep all for reconciliation
        winner = ok_results[0]
    elif strategy == "all" and all_ok:
        winner = ok_results[0]

    if strategy == "all":
        step["status"] = "completed" if all_ok else "failed"
    else:
        step["status"] = "completed" if any_ok else "failed"

    store.update_run(run_id)

    # Simple diff consensus hint: if multiple results provide a 'diff' string, pick the most common hash
    consensus = None
    diffs = []
    for r in ok_results:
        res = r.get("result") or {}
        d = res.get("diff")
        if isinstance(d, str) and d.strip():
            diffs.append(d)
    if diffs:
        counts = {}
        for d in diffs:
            h = hashlib.sha256(d.encode("utf-8")).hexdigest()
            counts[h] = counts.get(h, 0) + 1
        best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        consensus = {"diff_sha256": best[0], "votes": best[1], "total": len(diffs)}

    etype = "step.completed" if step["status"] == "completed" else "step.failed"
    payload = {"capability_id": cap, "strategy": strategy, "results": results, "winner": winner, "auto_artifacts": auto_artifacts}
    if consensus:
        payload["diff_consensus"] = consensus
    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type=etype, payload=payload))



# ---------------- Provider Event Publish (for streaming logs/progress) ----------------
@app.post("/runs/{run_id}/events/publish")
async def publish_event(run_id: str, event: Event, x_provider_token: str | None = Header(default=None)):
    _check_provider_token(x_provider_token)
    if run_id != event.run_id:
        raise HTTPException(400, "run_id mismatch")
    await emit_and_publish(event)
    return {"ok": True}

@app.websocket("/ws/runs/{run_id}")
async def ws_run(run_id: str, ws: WebSocket):
    await ws.accept()
    await hub.subscribe(run_id, ws)
    for e in store.events_by_run.get(run_id, []):
        await ws.send_json(e.model_dump())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.unsubscribe(run_id, ws)




# ---------------- Run Replay (best-effort) ----------------
@app.post("/runs/{run_id}/replay")
async def replay_run(run_id: str):
    if run_id not in store.runs:
        raise HTTPException(404, "run not found")
    orig = store.runs[run_id]
    new_run_id = new_id("run")
    title = f"Replay of {run_id}"
    run = RunRecord(run_id=new_run_id, project_id=orig.project_id, title=title, prompt=orig.prompt, access_profile=orig.access_profile)
    store.add_run(run)
    await emit_and_publish(Event(ts=now_iso(), run_id=new_run_id, type="run.started", payload={"title": title, "project_id": orig.project_id, "prompt": orig.prompt, "access_profile": orig.access_profile, "replay_of": run_id}))
    # clone steps and execute sequentially
    for s in orig.steps:
        step_id = new_id("step")
        step = {
            "step_id": step_id,
            "capability_id": s.get("capability_id"),
            "payload": s.get("payload") or {},
            "provider_id": s.get("provider_id"),
            "targets": s.get("targets"),
            "status": "planned",
            "created_at": now_iso(),
        }
        run.steps.append(step)
        store.update_run(new_run_id)
        await emit_and_publish(Event(ts=now_iso(), run_id=new_run_id, step_id=step_id, type="step.planned", payload={"capability_id": step["capability_id"], "provider_id": step.get("provider_id"), "targets": step.get("targets")}))
        await execute_step(new_run_id, step_id)
    await emit_and_publish(Event(ts=now_iso(), run_id=new_run_id, type="run.completed", payload={"replay_of": run_id}))
    return {"run_id": new_run_id}

# ---------------- Run Export Bundle ----------------
@app.get("/runs/{run_id}/export")
def export_run(run_id: str):
    if run_id not in store.runs:
        raise HTTPException(404, "run not found")
    run = store.runs[run_id].__dict__
    events = [e.model_dump() for e in store.events_by_run.get(run_id, [])]
    # Collect artifacts
    art_dir = os.path.join(store.data_dir, "artifacts", run_id)
    # Build zip in temp
    tmp_dir = tempfile.mkdtemp(prefix=f"run_export_{run_id}_")
    try:
        with open(os.path.join(tmp_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(run, f, indent=2)
        with open(os.path.join(tmp_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        # assets snapshot (current registry)
        with open(os.path.join(tmp_dir, "assets_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(store.assets, f, indent=2)
        # copy artifacts
        if os.path.isdir(art_dir):
            os.makedirs(os.path.join(tmp_dir, "artifacts"), exist_ok=True)
            for fn in os.listdir(art_dir):
                src = os.path.join(art_dir, fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(tmp_dir, "artifacts", fn))
        zip_path = os.path.join(tmp_dir, f"{run_id}_bundle.zip")
        with _zipfile.ZipFile(zip_path, "w", compression=_zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(tmp_dir):
                for fn in files:
                    if fn.endswith("_bundle.zip"):
                        continue
                    p = os.path.join(root, fn)
                    arc = os.path.relpath(p, tmp_dir)
                    z.write(p, arc)
        return FileResponse(zip_path, filename=f"{run_id}_bundle.zip")
    finally:
        # cleanup handled by OS eventually; keep minimal (do not remove before response)
        pass

# ---------------- Artifacts ----------------
@app.post("/artifacts/upload")
async def upload_artifact(run_id: str, type: str, file: UploadFile = File(...)):
    if run_id not in store.runs:
        raise HTTPException(404, "run not found")
    art_id = new_id("art")
    out_dir = os.path.join(store.data_dir, "artifacts", run_id)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{art_id}_{file.filename}")
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    meta = ArtifactMeta(
        artifact_id=art_id,
        type=type,
        created_at=now_iso(),
        filename=file.filename,
        content_type=file.content_type,
        meta={"path": path, "size": len(content)}
    )

    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, type="artifact.created", payload=meta.model_dump()))
    return meta.model_dump()


@app.get("/artifacts/list/{run_id}")
def list_artifacts(run_id: str):
    out_dir = os.path.join(store.data_dir, "artifacts", run_id)
    if not os.path.isdir(out_dir):
        return []
    files = []
    for fn in os.listdir(out_dir):
        files.append({"filename": fn})
    return files



@app.get("/artifacts/download/{run_id}/{filename}")
def download_artifact(run_id: str, filename: str):
    out_dir = os.path.join(store.data_dir, "artifacts", run_id)
    path = os.path.join(out_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "artifact not found")
    return FileResponse(path, filename=filename)



# ---------------- Automation Execution (interval-based v0) ----------------
_automation_task: Optional[asyncio.Task] = None
_automation_state: Dict[str, Any] = {"running": False, "last_tick": None}

def _now_ts() -> float:
    import time
    return time.time()

async def _run_automation_item(key: str, spec: Dict[str, Any]):
    # Spec: {title, project_id, prompt, access_profile, steps:[{capability_id, provider_id?, targets?, payload?}]}
    run_id = new_id("run")
    title = spec.get("title") or f"Automation {key}"
    run = RunRecord(run_id=run_id, project_id=spec.get("project_id"), title=title, prompt=spec.get("prompt"), access_profile=spec.get("access_profile"))
    store.add_run(run)
    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, type="run.started", payload={"title": title, "project_id": spec.get("project_id"), "prompt": spec.get("prompt"), "access_profile": spec.get("access_profile"), "automation_key": key}))
    for st in spec.get("steps", []):
        step_id = new_id("step")
        step = {"step_id": step_id, "capability_id": st.get("capability_id"), "payload": st.get("payload") or {}, "provider_id": st.get("provider_id"), "targets": st.get("targets"), "status": "planned", "created_at": now_iso()}
        run.steps.append(step); store.update_run(run_id)
        await emit_and_publish(Event(ts=now_iso(), run_id=run_id, step_id=step_id, type="step.planned", payload={"capability_id": step["capability_id"], "provider_id": step.get("provider_id"), "targets": step.get("targets")}))
        await execute_step(run_id, step_id)
    await emit_and_publish(Event(ts=now_iso(), run_id=run_id, type="run.completed", payload={"automation_key": key}))
    return run_id

async def _automation_loop():
    import time
    # schedule spec format: { interval_seconds: int, last_run_ts?: float, template: {...spec...} }
    while True:
        _automation_state["running"] = True
        _automation_state["last_tick"] = now_iso()
        schedules = store.automation.get("schedules", {})
        nowt = time.time()
        for key, sch in list(schedules.items()):
            try:
                interval = int((sch or {}).get("interval_seconds") or 0)
                if interval <= 0:
                    continue
                last = float((sch or {}).get("last_run_ts") or 0.0)
                if nowt - last >= interval:
                    tmpl = (sch or {}).get("template") or {}
                    store.automation_put("schedules", key, {**(sch or {}), "last_run_ts": nowt})
                    await _run_automation_item(key, tmpl)
            except Exception:
                continue
        await asyncio.sleep(2)

@app.post("/automation/runner/start")
async def automation_start():
    global _automation_task
    if _automation_task and not _automation_task.done():
        return {"ok": True, "running": True}
    _automation_task = asyncio.create_task(_automation_loop())
    return {"ok": True, "running": True}

@app.post("/automation/runner/stop")
async def automation_stop():
    global _automation_task
    if _automation_task:
        _automation_task.cancel()
    _automation_state["running"] = False
    return {"ok": True, "running": False}

@app.get("/automation/runner/status")
def automation_status():
    return _automation_state

@app.post("/automation/run/{key}")
async def automation_run_now(key: str):
    sch = store.automation.get("schedules", {}).get(key)
    if not sch:
        raise HTTPException(404, "schedule not found")
    tmpl = (sch or {}).get("template") or {}
    run_id = await _run_automation_item(key, tmpl)
    return {"ok": True, "run_id": run_id}


# ---------------- Serve minimal UI ----------------
UI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ui", "supercenter_web")
if os.path.isdir(UI_DIR):
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
