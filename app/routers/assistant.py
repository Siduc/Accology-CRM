"""Accologise AI assistant API (session-auth)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.assistant_actions import execute_plan
from app.services.assistant_brain import assistant_status, handle_chat
from app.services.assistant_plans import verify_plan_token
from app.services.demo_mode import is_demo_locked

router = APIRouter(prefix="/assistant", tags=["assistant"])


class ChatIn(BaseModel):
    message: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)
    page_context: dict[str, Any] = Field(default_factory=dict)
    plan_token: str = ""  # pending confirm token (voice "yes" support)


class ConfirmIn(BaseModel):
    token: str = ""
    accepted: bool = True


def _is_affirmative(text: str) -> bool:
    import re

    t = (text or "").strip().lower().rstrip(".!")
    return bool(
        re.fullmatch(
            r"(yes|y|yeah|yep|yup|ok|okay|sure|confirm|do it|go ahead|please do|affirmative|yeh|yea)(\s+please)?",
            t,
        )
    )


def _is_negative(text: str) -> bool:
    import re

    t = (text or "").strip().lower().rstrip(".!")
    return bool(
        re.fullmatch(r"(no|n|nope|cancel|stop|don't|do not|never mind|nevermind)", t)
    )


@router.get("/status")
async def status():
    return assistant_status()


@router.post("/chat")
async def chat(body: ChatIn, request: Request, db: Session = Depends(get_db)):
    # Spoken / typed Yes·No while a plan is open
    token = (body.plan_token or "").strip()
    if token and _is_affirmative(body.message or ""):
        if is_demo_locked(request):
            return JSONResponse(
                {
                    "kind": "message",
                    "reply": "Demo login cannot write to the database.",
                    "links": [],
                },
                status_code=403,
            )
        plan, err = verify_plan_token(token)
        if err or not plan:
            return JSONResponse(
                {"kind": "message", "reply": err or "That plan expired — ask again.", "links": []}
            )
        return JSONResponse(execute_plan(db, plan))
    if token and _is_negative(body.message or ""):
        return JSONResponse(
            {"kind": "message", "reply": "Cancelled — nothing was saved.", "links": []}
        )

    result = handle_chat(
        db,
        body.message,
        history=body.history,
        page_context=body.page_context,
    )
    return JSONResponse(result)


@router.post("/confirm")
async def confirm(body: ConfirmIn, request: Request, db: Session = Depends(get_db)):
    if not body.accepted:
        return JSONResponse(
            {
                "kind": "message",
                "reply": "Cancelled — nothing was saved.",
                "links": [],
            }
        )
    if is_demo_locked(request):
        return JSONResponse(
            {
                "kind": "message",
                "reply": "Demo login cannot write to the database. Sign in with a staff account to create records.",
                "links": [],
            },
            status_code=403,
        )
    plan, err = verify_plan_token(body.token or "")
    if err or not plan:
        return JSONResponse(
            {"kind": "message", "reply": err or "Invalid plan", "links": []},
            status_code=400,
        )
    result = execute_plan(db, plan)
    return JSONResponse(result)
