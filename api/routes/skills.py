"""
/v1/skills       — Vibe Work skills (tRPC skills.*)
/v1/tasks        — Scheduled tasks (tRPC scheduledTask.*)
/v1/workflows    — Workflows (tRPC workflow.*)
/v1/projects     — Projects (tRPC project.*)
/v1/organizations — Organizations (tRPC organization.all)
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from api.client import get_client

router = APIRouter(tags=["skills & tasks"])


def _trpc_get(client, proc: str, payload: dict) -> Any:
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    r = client.session.get(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1&input={inp}",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:300]}")
    data = r.json()
    if isinstance(data, list) and data:
        if "result" in data[0]:
            return data[0]["result"]["data"]["json"]
        if "error" in data[0]:
            raise HTTPException(status_code=400, detail=data[0]["error"]["json"]["message"])
    return data


def _trpc_post(client, proc: str, payload: dict) -> Any:
    r = client.session.post(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1",
        json={"0": {"json": payload}},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:300]}")
    data = r.json()
    if isinstance(data, list) and data:
        if "result" in data[0]:
            return data[0]["result"]["data"]["json"]
        if "error" in data[0]:
            raise HTTPException(status_code=400, detail=data[0]["error"]["json"]["message"])
    return data


def _auth(client):
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")


# ── Skills (Vibe Work) ────────────────────────────────────────────────────────

@router.get("/v1/skills")
def list_skills(
    search: Optional[str] = Query(None, description="Search string"),
    process: Optional[str] = Query(None, description="Filter by process"),
    for_chat: bool = Query(False, description="Use listForChat (returns 3 active skills)"),
):
    """
    List Vibe Work skills.
    Returns 12 skills via skills.list (all) or 3 active via skills.listForChat.
    Skills are registered under feature flag le_chat_registry_skills.
    Each skill has: id, title, description, body (markdown system prompt), process,
    extra._vibe_enable_state, extra._vibe_product_surfaces.
    """
    client = get_client()
    _auth(client)
    payload: dict = {}
    if search:
        payload["searchString"] = search
    if process:
        payload["process"] = process
    proc = "skills.listForChat" if for_chat else "skills.list"
    return _trpc_get(client, proc, payload)


@router.get("/v1/skills/{skill_id}")
def get_skill(skill_id: str):
    """
    Get a skill by UUID (tRPC skills.get).
    Returns full skill including the markdown body (system prompt content).
    skill_id must be a UUID — use GET /v1/skills to find IDs.
    """
    client = get_client()
    _auth(client)
    return _trpc_get(client, "skills.get", {"skillId": skill_id})


# ── Scheduled Tasks ────────────────────────────────────────────────────────────

@router.get("/v1/tasks")
def list_tasks(
    status: Optional[str] = Query(None, description="Filter: scheduled | paused | completed"),
    page: int = Query(0, description="Page number"),
    page_size: int = Query(20, description="Items per page"),
):
    """
    List scheduled tasks (tRPC scheduledTask.list).
    Feature flag: scheduled_task=true.
    Free accounts: limit=5 active tasks.
    Status values: scheduled | paused | completed (not 'active').
    """
    client = get_client()
    _auth(client)
    payload: dict = {"page": page, "pageSize": page_size}
    if status:
        payload["status"] = status
    return _trpc_get(client, "scheduledTask.list", payload)


@router.get("/v1/tasks/{task_id}")
def get_task(task_id: str):
    """Get a scheduled task by ID (tRPC scheduledTask.getById)."""
    client = get_client()
    _auth(client)
    return _trpc_get(client, "scheduledTask.getById", {"id": task_id})


@router.get("/v1/tasks/{task_id}/executions")
def get_task_executions(task_id: str):
    """Get latest executions for a scheduled task (tRPC scheduledTask.listLatestExecutions)."""
    client = get_client()
    _auth(client)
    return _trpc_get(client, "scheduledTask.listLatestExecutions", {"id": task_id})


@router.post("/v1/tasks/{task_id}/trigger")
def trigger_task(task_id: str):
    """Manually trigger a scheduled task (tRPC scheduledTask.trigger)."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "scheduledTask.trigger", {"id": task_id})


# ── Workflows ─────────────────────────────────────────────────────────────────

@router.get("/v1/workflows")
def list_workflows():
    """
    List workflows (tRPC workflow.getWorkflows).
    Feature flag: workflows_ui_enabled=true.
    Returns {workflows: []}.
    """
    client = get_client()
    _auth(client)
    return _trpc_get(client, "workflow.getWorkflows", {})


@router.get("/v1/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    """Get a workflow by version ID (tRPC workflow.getWorkflow). Uses workflowVersionId, not a plain id."""
    client = get_client()
    _auth(client)
    return _trpc_get(client, "workflow.getWorkflow", {"workflowVersionId": workflow_id})


class WorkflowStartBody(BaseModel):
    workflow_version_id: str
    input: Optional[dict] = None
    assistant_message_key: Optional[dict] = None


@router.post("/v1/workflows/start")
def start_workflow(body: WorkflowStartBody):
    """
    Start a workflow execution (tRPC workflow.startWorkflow).
    workflow_version_id: the workflowVersionId from workflow.getWorkflows response.
    input: dict of input values for the workflow.
    assistant_message_key: required object (opaque; get from workflow metadata).
    """
    client = get_client()
    _auth(client)
    payload: dict = {"workflowVersionId": body.workflow_version_id}
    if body.input is not None:
        payload["input"] = body.input
    if body.assistant_message_key is not None:
        payload["assistantMessageKey"] = body.assistant_message_key
    return _trpc_post(client, "workflow.startWorkflow", payload)


class WorkflowInputBody(BaseModel):
    workflow_version_id: str
    execution_id: str
    custom_task_id: str
    input: str


@router.post("/v1/workflows/{workflow_id}/input")
def send_workflow_input(workflow_id: str, body: WorkflowInputBody):
    """
    Send user input to a running workflow (tRPC workflow.sendUserInput).
    workflow_version_id, execution_id (workflowExecutionId), custom_task_id:
    all obtained from the workflow execution state.
    """
    client = get_client()
    _auth(client)
    return _trpc_post(client, "workflow.sendUserInput", {
        "workflowVersionId": body.workflow_version_id,
        "workflowExecutionId": body.execution_id,
        "customTaskId": body.custom_task_id,
        "input": body.input,
    })


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/v1/projects")
def list_projects():
    """
    List projects (tRPC project.list).
    Free accounts: maxProjects=2.
    Returns array of project objects.
    """
    client = get_client()
    _auth(client)
    return _trpc_get(client, "project.list", {})


@router.get("/v1/projects/limits")
def get_project_limits():
    """Get project limits (tRPC project.limits) — e.g. maxProjects=2 for free."""
    client = get_client()
    _auth(client)
    return _trpc_get(client, "project.limits", {})


@router.get("/v1/projects/{project_id}")
def get_project(project_id: str):
    """Get a project by ID (tRPC project.byId)."""
    client = get_client()
    _auth(client)
    return _trpc_get(client, "project.byId", {"id": project_id})


class ProjectChatBody(BaseModel):
    chat_id: str


@router.post("/v1/projects/{project_id}/chats")
def add_chat_to_project(project_id: str, body: ProjectChatBody):
    """Add a chat to a project (tRPC project.addChat)."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "project.addChat", {
        "projectId": project_id,
        "chatId": body.chat_id,
    })


@router.delete("/v1/projects/{project_id}/chats/{chat_id}")
def remove_chat_from_project(project_id: str, chat_id: str):
    """Remove a chat from a project (tRPC project.removeChat). Only chatId is required."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "project.removeChat", {"chatId": chat_id})


@router.delete("/v1/projects/{project_id}")
def delete_project(project_id: str):
    """Delete a project (tRPC project.delete)."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "project.delete", {"id": project_id})


# ── Organizations ─────────────────────────────────────────────────────────────

@router.get("/v1/organizations")
def list_organizations():
    """
    List user's organizations (tRPC organization.all).
    Returns {items: [...], nextCursor: null}.
    Each org has: id, name, customerId, leChatPlan, orgTier (B=free), role, isLeChatPro.
    """
    client = get_client()
    _auth(client)
    return _trpc_get(client, "organization.all", {})


# ── Billing ───────────────────────────────────────────────────────────────────

@router.get("/v1/billing/pro")
def get_pro_description():
    """
    Get Le Chat Pro plan description (tRPC billing.leChatProDescription).
    Returns {individual: {localizedTitle, localizedFeatures: [...]}} with plan benefits.
    """
    client = get_client()
    _auth(client)
    return _trpc_get(client, "billing.leChatProDescription", {})


# ── Scheduled Task pause/resume/delete ────────────────────────────────────────

@router.post("/v1/tasks/{task_id}/pause")
def pause_task(task_id: str):
    """Pause a scheduled task (tRPC scheduledTask.pause)."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "scheduledTask.pause", {"id": task_id})


@router.post("/v1/tasks/{task_id}/resume")
def resume_task(task_id: str):
    """Resume a paused scheduled task (tRPC scheduledTask.resume)."""
    client = get_client()
    _auth(client)
    return _trpc_post(client, "scheduledTask.resume", {"id": task_id})


@router.delete("/v1/tasks/{task_id}")
def delete_task(task_id: str):
    """
    Delete a scheduled task (tRPC scheduledTask.delete).
    Warning: chats from this task will be archived.
    """
    client = get_client()
    _auth(client)
    return _trpc_post(client, "scheduledTask.delete", {"id": task_id})
