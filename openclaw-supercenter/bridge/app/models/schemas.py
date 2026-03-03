
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class ProviderManifest(BaseModel):
    provider_id: str
    display_name: Optional[str] = None
    base_url: str
    capabilities: List[str]
    constraints: Dict[str, Any] = Field(default_factory=dict)
    supports_streaming: bool = False
    version: Optional[str] = None


class RunCreate(BaseModel):
    project_id: Optional[str] = None
    title: Optional[str] = None
    prompt: Optional[str] = None
    access_profile: str = "standard"


class RunStepCreate(BaseModel):
    capability_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    provider_id: Optional[str] = None
    targets: Optional[List[str]] = None  # provider_ids for multi-target routing
    requires_approval: Optional[bool] = None  # override, rarely used


class Event(BaseModel):
    ts: str
    run_id: str
    step_id: Optional[str] = None
    type: str
    provider_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class ArtifactMeta(BaseModel):
    artifact_id: str
    type: str
    created_at: str
    filename: Optional[str] = None
    content_type: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class ProjectCreate(BaseModel):
    name: str
    repos: List[Dict[str, Any]] = Field(default_factory=list)
    workspace_prefs: Dict[str, Any] = Field(default_factory=dict)
    default_mode: Optional[str] = None
    provider_pins: Dict[str, str] = Field(default_factory=dict)
    access_profile: str = "standard"


class AssetPut(BaseModel):
    key: str
    value: Dict[str, Any]


class ApprovalCreate(BaseModel):
    run_id: str
    step_id: Optional[str] = None
    capability_id: str
    requested_by: Optional[str] = None
    reason: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class ApprovalRespond(BaseModel):
    decision: str  # approved | denied
    note: Optional[str] = None


class SecretCreate(BaseModel):
    name: str
    value: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class SecretBind(BaseModel):
    project_id: str


class AutomationPut(BaseModel):
    key: str
    value: Dict[str, Any]
