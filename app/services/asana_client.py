"""Thin Asana REST API client (Personal Access Token)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import (
    ASANA_ACCESS_TOKEN,
    ASANA_PROJECT_GID,
    ASANA_WORKSPACE_GID,
)

logger = logging.getLogger("accountant_crm.asana")

ASANA_API = "https://app.asana.com/api/1.0"


@dataclass
class AsanaResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class AsanaTaskView:
    gid: str
    name: str
    completed: bool
    due_on: Optional[str] = None
    notes: str = ""
    permalink_url: str = ""


def is_configured() -> bool:
    return bool((ASANA_ACCESS_TOKEN or "").strip())


def workspace_configured() -> bool:
    return is_configured() and bool((ASANA_WORKSPACE_GID or "").strip())


def _token() -> str:
    return (ASANA_ACCESS_TOKEN or "").strip()


def _request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> AsanaResult:
    token = _token()
    if not token:
        return AsanaResult(ok=False, error="Asana token not configured (ASANA_ACCESS_TOKEN).")

    url = f"{ASANA_API}{path}"
    if params:
        # Flatten list params for Asana opt_fields etc.
        flat = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                for item in v:
                    flat.append((k, str(item)))
            else:
                flat.append((k, str(v)))
        url = f"{url}?{urlencode(flat)}"

    data_bytes = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data_bytes, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
            return AsanaResult(ok=True, data=payload.get("data") or payload)
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
            detail = json.loads(err_body)
            msg = detail.get("errors", detail)
            if isinstance(msg, list) and msg:
                msg = msg[0].get("message") or str(msg[0])
        except Exception:  # noqa: BLE001
            msg = str(exc.reason or exc)
        if exc.code == 401:
            return AsanaResult(ok=False, error="Asana: unauthorized — check ASANA_ACCESS_TOKEN.")
        if exc.code == 403:
            return AsanaResult(ok=False, error="Asana: forbidden — check workspace access.")
        if exc.code == 429:
            return AsanaResult(ok=False, error="Asana: rate limited — try again shortly.")
        return AsanaResult(ok=False, error=f"Asana HTTP {exc.code}: {msg}")
    except URLError as exc:
        return AsanaResult(ok=False, error=f"Asana network error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Asana request failed")
        return AsanaResult(ok=False, error=str(exc))


def test_connection() -> AsanaResult:
    """GET /users/me — returns user dict on success."""
    return _request("GET", "/users/me")


def list_my_incomplete_tasks(
    *,
    workspace_gid: Optional[str] = None,
    limit: int = 50,
) -> AsanaResult:
    """
    Incomplete tasks assigned to the token user in the workspace.
    Returns list in result.data as list of task dicts.
    """
    ws = (workspace_gid or ASANA_WORKSPACE_GID or "").strip()
    if not ws:
        return AsanaResult(ok=False, error="ASANA_WORKSPACE_GID is required to list My Tasks.")

    # Use user task list for assignee=me
    # GET /users/me/user_task_list?workspace=
    utl = _request(
        "GET",
        "/users/me/user_task_list",
        params={"workspace": ws},
    )
    if not utl.ok:
        # Fallback: search tasks
        return _list_tasks_fallback(ws, limit)

    list_gid = (utl.data or {}).get("gid")
    if not list_gid:
        return _list_tasks_fallback(ws, limit)

    res = _request(
        "GET",
        f"/user_task_lists/{list_gid}/tasks",
        params={
            "completed_since": "now",  # incomplete only
            "opt_fields": "name,completed,due_on,notes,permalink_url",
            "limit": min(limit, 100),
        },
    )
    if not res.ok:
        return res
    # data may be list
    tasks = res.data if isinstance(res.data, list) else []
    return AsanaResult(ok=True, data={"tasks": tasks})


def _list_tasks_fallback(workspace_gid: str, limit: int) -> AsanaResult:
    res = _request(
        "GET",
        "/tasks",
        params={
            "assignee": "me",
            "workspace": workspace_gid,
            "completed_since": "now",
            "opt_fields": "name,completed,due_on,notes,permalink_url",
            "limit": min(limit, 100),
        },
    )
    if not res.ok:
        return res
    tasks = res.data if isinstance(res.data, list) else []
    return AsanaResult(ok=True, data={"tasks": tasks})


def create_task(
    *,
    name: str,
    notes: str = "",
    due_on: Optional[str] = None,
    workspace_gid: Optional[str] = None,
    project_gid: Optional[str] = None,
    assignee: str = "me",
) -> AsanaResult:
    ws = (workspace_gid or ASANA_WORKSPACE_GID or "").strip()
    if not ws:
        return AsanaResult(ok=False, error="ASANA_WORKSPACE_GID is required to create tasks.")

    payload: Dict[str, Any] = {
        "name": name[:1024],
        "notes": notes or "",
        "workspace": ws,
        "assignee": assignee,
    }
    if due_on:
        payload["due_on"] = due_on
    proj = (project_gid or ASANA_PROJECT_GID or "").strip()
    if proj:
        payload["projects"] = [proj]

    return _request("POST", "/tasks", body={"data": payload})


def update_task(
    task_gid: str,
    *,
    completed: Optional[bool] = None,
    due_on: Optional[str] = None,
    name: Optional[str] = None,
    notes: Optional[str] = None,
) -> AsanaResult:
    data: Dict[str, Any] = {}
    if completed is not None:
        data["completed"] = bool(completed)
    if due_on is not None:
        data["due_on"] = due_on or None
    if name is not None:
        data["name"] = name[:1024]
    if notes is not None:
        data["notes"] = notes
    if not data:
        return AsanaResult(ok=False, error="Nothing to update.")
    return _request("PUT", f"/tasks/{task_gid}", body={"data": data})


def get_task(task_gid: str) -> AsanaResult:
    return _request(
        "GET",
        f"/tasks/{task_gid}",
        params={"opt_fields": "name,completed,due_on,notes,permalink_url"},
    )


def tasks_to_views(raw_tasks: List[Dict[str, Any]]) -> List[AsanaTaskView]:
    out: List[AsanaTaskView] = []
    for t in raw_tasks or []:
        out.append(
            AsanaTaskView(
                gid=str(t.get("gid") or ""),
                name=t.get("name") or "(untitled)",
                completed=bool(t.get("completed")),
                due_on=t.get("due_on"),
                notes=(t.get("notes") or "")[:200],
                permalink_url=t.get("permalink_url") or "",
            )
        )
    return out
