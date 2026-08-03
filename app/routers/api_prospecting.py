"""
JSON API: Outlook / Power Automate → prospect activity + follow-up task + OneDrive.

Auth: same X-API-Key as task push (TASK_IMPORT_API_KEY).
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import TASK_PUSH_API_KEY
from app.database import get_db
from app.services.email_prospect_push import parse_payload, process_prospect_email

router = APIRouter(prefix="/api/v1", tags=["api-prospecting"])


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
    expected = (TASK_PUSH_API_KEY or "").strip()
    if not expected:
        return _json_error(
            503,
            "API not configured. Set TASK_IMPORT_API_KEY and restart.",
        )
    provided = _extract_api_key(authorization, x_api_key)
    if not provided:
        return _json_error(401, "Missing X-API-Key header.")
    if len(provided) != len(expected) or not secrets.compare_digest(provided, expected):
        return _json_error(401, "Invalid API key.")
    return None


@router.get("/prospecting/from-email")
async def from_email_help():
    return JSONResponse(
        content={
            "ok": True,
            "endpoint": "POST /api/v1/prospecting/from-email",
            "auth_header": "X-API-Key",
            "auth_env": "TASK_IMPORT_API_KEY",
            "purpose": (
                "Flag a sent/received prospect email: log activity, set pipeline value, "
                "create follow-up task, store proposal attachments in OneDrive "
                "(Accologise / Prospects / {Name} / Proposals)."
            ),
            "body": {
                "subject": "required",
                "to": "recipient(s) — used to match Prospect.email",
                "from": "your mailbox",
                "body_preview": "optional",
                "prospect_id": "optional explicit match",
                "company_name": "optional match fallback",
                "estimated_value": "optional e.g. 10000 or £10,000",
                "follow_up": "true (default) — create task",
                "follow_up_days": "7 default",
                "direction": "outbound (default) | inbound",
                "message_id": "Graph id",
                "web_link": "Outlook web link",
                "attachments": [
                    {
                        "filename": "Proposal.pdf",
                        "content_base64": "<base64>",
                        "content_type": "application/pdf",
                    }
                ],
            },
            "onedrive_path": "Accologise / Prospects / {Prospect} / Proposals",
            "configured": bool((TASK_PUSH_API_KEY or "").strip()),
        }
    )


@router.api_route("/prospecting/from-email", methods=["POST", "OPTIONS"])
@router.api_route("/prospecting/from-email/", methods=["POST", "OPTIONS"])
async def push_email_to_prospect(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    if request.method == "OPTIONS":
        return JSONResponse(content={"ok": True})

    denied = _require_api_key(authorization, x_api_key)
    if denied:
        return denied

    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return _json_error(400, "JSON body required")

    payload, errors = parse_payload(data)
    if errors or not payload:
        return _json_error(400, *(errors or ["Invalid payload"]))

    result = process_prospect_email(db, payload, uploaded_by="outlook_push")
    status = 200 if result.ok else 422
    return JSONResponse(status_code=status, content=result.as_dict())
