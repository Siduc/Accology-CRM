"""
JSON API for selective Outlook → Task push (Power Automate / Outlook button).

No browser session required. Auth is API key only:
  X-API-Key: <TASK_IMPORT_API_KEY>
  or Authorization: Bearer <TASK_IMPORT_API_KEY>

All responses are JSON (never redirects).
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.config import TASK_PUSH_API_KEY
from app.database import get_db
from app.services.email_task_push import create_task_from_email, parse_payload

router = APIRouter(prefix="/api/v1", tags=["api-tasks"])


def _extract_api_key(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> str:
    if x_api_key and str(x_api_key).strip():
        return str(x_api_key).strip()
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.lower().startswith("apikey "):
        return auth[7:].strip()
    # Some clients send the raw key as Authorization without Bearer
    if auth and " " not in auth:
        return auth
    return ""


def _json_error(status: int, *messages: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "errors": [m for m in messages if m]},
    )


def _require_api_key(
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> Optional[JSONResponse]:
    """Return a JSON error response if the key is missing/invalid; else None."""
    expected = (TASK_PUSH_API_KEY or "").strip()
    if not expected:
        return _json_error(
            503,
            "Task push API is not configured. "
            "Set TASK_IMPORT_API_KEY (or ACCOLOGISE_TASK_PUSH_KEY) in the environment and restart.",
        )
    provided = _extract_api_key(authorization, x_api_key)
    if not provided:
        return _json_error(
            401,
            "Missing API key. Send header X-API-Key with your TASK_IMPORT_API_KEY value.",
        )
    # compare_digest requires equal length; pad check avoids exception on mismatch lengths
    if len(provided) != len(expected) or not secrets.compare_digest(provided, expected):
        return _json_error(401, "Invalid API key.")
    return None


@router.get("/tasks/from-email")
async def from_email_help(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Contract for Power Automate. GET is open (no key) so you can probe the URL;
    POST always requires X-API-Key.
    """
    return JSONResponse(
        content={
            "ok": True,
            "endpoint": "POST /api/v1/tasks/from-email",
            "auth_header": "X-API-Key",
            "auth_env": "TASK_IMPORT_API_KEY",
            "auth_alt": "Authorization: Bearer <TASK_IMPORT_API_KEY>",
            "session_required": False,
            "configured": bool((TASK_PUSH_API_KEY or "").strip()),
            "body": {
                "subject": "required — becomes task title",
                "from": "Name <email@domain> or email",
                "to": "optional",
                "received_at": "ISO datetime",
                "body_preview": "short plain text",
                "body": "optional fuller text",
                "message_id": "Graph message id (dedupe + Outlook link)",
                "conversation_id": "optional",
                "web_link": "optional Outlook web link",
                "priority": "High | Medium | Low",
            },
        }
    )


@router.api_route("/tasks/from-email", methods=["POST", "OPTIONS"])
@router.api_route("/tasks/from-email/", methods=["POST", "OPTIONS"])
async def push_email_to_task(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Create a Task from a deliberately pushed Outlook email.

    Auth: X-API-Key (preferred) or Bearer — no browser session.
    Middleware also enforces the key for POST and never 303-redirects /api/*.
    Always returns JSON (never a redirect).
    """
    if request.method == "OPTIONS":
        # Power Automate / CORS preflight — allow key header
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-API-Key, Authorization",
            },
        )

    # Prefer key already validated in middleware; re-check for defence in depth
    if not getattr(request.state, "api_key_ok", False):
        auth_err = _require_api_key(authorization, x_api_key)
        if auth_err is not None:
            return auth_err

    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body")

    if not isinstance(data, dict):
        return _json_error(400, "Body must be a JSON object")

    payload, errors = parse_payload(data)
    if errors or not payload:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "errors": errors or ["Invalid payload"]},
        )

    try:
        result = create_task_from_email(db, payload)
    except Exception as exc:  # noqa: BLE001
        return _json_error(500, f"Server error creating task: {exc}")

    status = 201 if result.created else (200 if result.ok else 400)
    body = result.as_dict()
    body["auth"] = "api_key"
    return JSONResponse(status_code=status, content=body)
