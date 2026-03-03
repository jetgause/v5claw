
from __future__ import annotations

import os
import uuid
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..models.schemas import ProviderManifest, Event

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

@dataclass
class ProviderRecord:
    manifest: ProviderManifest
    last_seen: str = field(default_factory=now_iso)

@dataclass
class RunRecord:
    run_id: str
    project_id: Optional[str]
    title: str
    prompt: Optional[str]
    access_profile: str
    created_at: str = field(default_factory=now_iso)
    status: str = "running"
    steps: List[Dict[str, Any]] = field(default_factory=list)

class JsonStore:
    """
    Lightweight persistence store (JSON on disk) to keep the bridge usable without a DB.
    The design keeps things simple and transparent. It is not meant for high-concurrency deployments.
    """
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "artifacts"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "state"), exist_ok=True)

        self._paths = {
            "providers": os.path.join(self.data_dir, "state", "providers.json"),
            "runs": os.path.join(self.data_dir, "state", "runs.json"),
            "events": os.path.join(self.data_dir, "state", "events.json"),
            "projects": os.path.join(self.data_dir, "state", "projects.json"),
            "assets": os.path.join(self.data_dir, "state", "assets.json"),
            "approvals": os.path.join(self.data_dir, "state", "approvals.json"),
            "secrets": os.path.join(self.data_dir, "state", "secrets.json"),
            "automation": os.path.join(self.data_dir, "state", "automation.json"),
        }

        self.providers: Dict[str, ProviderRecord] = {}
        self.runs: Dict[str, RunRecord] = {}
        self.events_by_run: Dict[str, List[Event]] = {}
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.assets: Dict[str, Dict[str, Any]] = {"modes": {}, "skills": {}, "tools": {}, "agents": {}}
        self.approvals: Dict[str, Dict[str, Any]] = {}  # approval_id -> record
        self.secrets: Dict[str, Dict[str, Any]] = {}    # handle_id -> record
        self.automation: Dict[str, Dict[str, Any]] = {"schedules": {}, "triggers": {}, "watchers": {}, "templates": {}}

        self._load_all()

    def _read_json(self, path: str, default: Any):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write_json(self, path: str, obj: Any):
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False)
        os.replace(tmp, path)

    def _load_all(self):
        prov = self._read_json(self._paths["providers"], {})
        for pid, rec in prov.items():
            try:
                self.providers[pid] = ProviderRecord(
                    manifest=ProviderManifest(**rec["manifest"]),
                    last_seen=rec.get("last_seen", now_iso()),
                )
            except Exception:
                continue

        runs = self._read_json(self._paths["runs"], {})
        for rid, r in runs.items():
            try:
                self.runs[rid] = RunRecord(**r)
            except Exception:
                continue

        events = self._read_json(self._paths["events"], {})
        for rid, evs in events.items():
            self.events_by_run[rid] = []
            for e in evs:
                try:
                    self.events_by_run[rid].append(Event(**e))
                except Exception:
                    continue

        self.projects = self._read_json(self._paths["projects"], {})
        self.assets = self._read_json(self._paths["assets"], self.assets)
        self.approvals = self._read_json(self._paths["approvals"], {})
        self.secrets = self._read_json(self._paths["secrets"], {})
        self.automation = self._read_json(self._paths["automation"], self.automation)

    def _persist_providers(self):
        blob = {pid: {"manifest": rec.manifest.model_dump(), "last_seen": rec.last_seen} for pid, rec in self.providers.items()}
        self._write_json(self._paths["providers"], blob)

    def _persist_runs(self):
        blob = {rid: asdict(r) for rid, r in self.runs.items()}
        self._write_json(self._paths["runs"], blob)

    def _persist_events(self):
        blob = {rid: [e.model_dump() for e in evs] for rid, evs in self.events_by_run.items()}
        self._write_json(self._paths["events"], blob)

    def _persist_projects(self):
        self._write_json(self._paths["projects"], self.projects)

    def _persist_assets(self):
        self._write_json(self._paths["assets"], self.assets)

    def _persist_approvals(self):
        self._write_json(self._paths["approvals"], self.approvals)

    def _persist_secrets(self):
        self._write_json(self._paths["secrets"], self.secrets)

    def _persist_automation(self):
        self._write_json(self._paths["automation"], self.automation)

    # Providers
    def add_provider(self, manifest: ProviderManifest):
        self.providers[manifest.provider_id] = ProviderRecord(manifest=manifest)
        self._persist_providers()

    def touch_provider(self, provider_id: str):
        if provider_id in self.providers:
            self.providers[provider_id].last_seen = now_iso()
            self._persist_providers()

    # Projects
    def add_project(self, proj: Dict[str, Any]) -> str:
        pid = proj.get("project_id") or new_id("proj")
        proj["project_id"] = pid
        self.projects[pid] = proj
        self._persist_projects()
        return pid

    # Runs
    def add_run(self, run: RunRecord):
        self.runs[run.run_id] = run
        self.events_by_run.setdefault(run.run_id, [])
        self._persist_runs()
        self._persist_events()

    def update_run(self, run_id: str):
        self._persist_runs()

    def append_event(self, event: Event):
        self.events_by_run.setdefault(event.run_id, []).append(event)
        self._persist_events()

    # Assets
    def asset_put(self, kind: str, key: str, obj: Dict[str, Any]):
        self.assets.setdefault(kind, {})[key] = obj
        self._persist_assets()

    def asset_delete(self, kind: str, key: str):
        if key in self.assets.get(kind, {}):
            del self.assets[kind][key]
            self._persist_assets()

    # Approvals
    def approval_create(self, record: Dict[str, Any]) -> str:
        aid = record.get("approval_id") or new_id("appr")
        record["approval_id"] = aid
        self.approvals[aid] = record
        self._persist_approvals()
        return aid

    def approval_update(self, approval_id: str, patch: Dict[str, Any]):
        if approval_id not in self.approvals:
            return
        self.approvals[approval_id].update(patch)
        self._persist_approvals()

    # Secrets (handles only; values are stored as plain text for v0 - DO NOT USE in production)
    def secret_create(self, record: Dict[str, Any]) -> str:
        sid = record.get("handle_id") or new_id("sec")
        record["handle_id"] = sid
        self.secrets[sid] = record
        self._persist_secrets()
        return sid

    def secret_update(self, handle_id: str, patch: Dict[str, Any]):
        if handle_id not in self.secrets:
            return
        self.secrets[handle_id].update(patch)
        self._persist_secrets()

    # Automation (basic CRUD)
    def automation_put(self, kind: str, key: str, obj: Dict[str, Any]):
        self.automation.setdefault(kind, {})[key] = obj
        self._persist_automation()

    def automation_delete(self, kind: str, key: str):
        if key in self.automation.get(kind, {}):
            del self.automation[kind][key]
            self._persist_automation()
