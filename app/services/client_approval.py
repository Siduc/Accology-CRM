"""Signed client-approval links. No email send. No filings."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.models.job import Job

MAX_AGE_SECONDS = 14 * 24 * 3600
PURPOSE = "client-job-approval"


def _serializer() -> URLSafeTimedSerializer:
    from app import config

    secret = (getattr(config, "SESSION_SECRET", None) or "dev-only-change-me").strip()
    return URLSafeTimedSerializer(secret, salt="accologise-client-approval")


def mint_token(job_id: int) -> str:
    return _serializer().dumps({"job_id": int(job_id), "purpose": PURPOSE})


def read_token(token: str) -> Optional[int]:
    try:
        data = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("purpose") != PURPOSE:
        return None
    try:
        return int(data.get("job_id"))
    except (TypeError, ValueError):
        return None


def apply_decision(
    db: Session,
    job: Job,
    *,
    decision: str,
    actor: str = "",
) -> Job:
    d = (decision or "").strip().lower()
    if d not in ("approved", "declined"):
        raise ValueError("decision must be approved or declined")
    job.client_approval_status = d
    job.client_approval_at = datetime.utcnow()
    job.client_approval_by = (actor or "").strip() or None
    job.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def mark_sent(db: Session, job: Job) -> Job:
    if (job.client_approval_status or "") not in ("approved", "declined"):
        job.client_approval_status = "sent"
        job.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
    return job
