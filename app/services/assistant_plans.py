"""
Signed pending-action plans for Accologise AI.

Plans are assembled during chat (read-only tools + NLU), signed with HMAC,
shown to the user for confirmation, then executed only after Yes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import AI_PLAN_SECRET

PLAN_TTL_SECONDS = 30 * 60  # 30 minutes


@dataclass
class PlanStep:
    op: str
    label: str
    detail: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingPlan:
    summary: str
    steps: List[PlanStep]
    preview: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "summary": self.summary,
            "steps": [
                {
                    "op": s.op,
                    "label": s.label,
                    "detail": s.detail,
                    "params": s.params,
                }
                for s in self.steps
            ],
            "preview": self.preview,
            "created_at": self.created_at or time.time(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingPlan":
        steps = [
            PlanStep(
                op=s.get("op") or "",
                label=s.get("label") or "",
                detail=s.get("detail") or "",
                params=dict(s.get("params") or {}),
            )
            for s in (data.get("steps") or [])
        ]
        return cls(
            summary=data.get("summary") or "",
            steps=steps,
            preview=dict(data.get("preview") or {}),
            created_at=float(data.get("created_at") or 0),
            version=int(data.get("version") or 1),
        )


def _secret_bytes() -> bytes:
    return (AI_PLAN_SECRET or "dev-ai-plan-secret").encode("utf-8")


def sign_plan(plan: PendingPlan) -> str:
    """Return URL-safe token: base64(payload).signature"""
    payload = plan.to_dict()
    if not payload.get("created_at"):
        payload["created_at"] = time.time()
        plan.created_at = payload["created_at"]
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_secret_bytes(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_plan_token(token: str) -> Tuple[Optional[PendingPlan], str]:
    """Verify signature and TTL. Returns (plan, error_message)."""
    if not token or "." not in token:
        return None, "Invalid plan token"
    body, _, sig = token.partition(".")
    if not body or not sig:
        return None, "Invalid plan token"
    expected = hmac.new(
        _secret_bytes(), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None, "Plan signature invalid or tampered"
    try:
        pad = "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(body + pad)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "Could not decode plan"
    created = float(data.get("created_at") or 0)
    if created and (time.time() - created) > PLAN_TTL_SECONDS:
        return None, "This plan has expired — ask again"
    plan = PendingPlan.from_dict(data)
    if not plan.steps:
        return None, "Plan has no steps"
    return plan, ""


def resolve_relative_date(text: str, today: Optional[date] = None) -> Optional[date]:
    """
    Parse common UK relative / absolute due dates.
    Examples: next Friday, tomorrow, in 3 days, Friday, 2026-08-07, 7/8/2026
    """
    today = today or date.today()
    s = (text or "").strip().lower()
    if not s:
        return None

    # ISO
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # d/m/yyyy or d-m-yyyy
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    if s in ("today",):
        return today
    if s in ("tomorrow",):
        return today + timedelta(days=1)

    m = re.fullmatch(r"in\s+(\d+)\s+days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))

    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    next_m = re.fullmatch(r"next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", s)
    if next_m:
        target = weekdays[next_m.group(1)]
        # Always strictly after today into the next occurrence (if today is Friday, next Friday = +7)
        days_ahead = (target - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    bare = re.fullmatch(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", s)
    if bare:
        target = weekdays[bare.group(1)]
        days_ahead = (target - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    # "end of week" / "this friday"
    this_m = re.fullmatch(r"this\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", s)
    if this_m:
        target = weekdays[this_m.group(1)]
        days_ahead = (target - today.weekday() + 7) % 7
        return today + timedelta(days=days_ahead)

    return None


def extract_company_number(text: str) -> Optional[str]:
    """Pull a likely UK company number from free text."""
    from app.services.company_numbers import normalize_company_number

    s = text or ""
    # Explicit "company number 12345678" / "CN 12345678" / "no. 12345678"
    m = re.search(
        r"(?:company\s*(?:number|no\.?|#)|c\.?n\.?|companies\s*house)\s*[:\s#]*([A-Z]{0,2}\d{6,8})",
        s,
        re.I,
    )
    if m:
        return normalize_company_number(m.group(1))
    # Bare 8-digit or SC/OC/etc + digits
    m = re.search(r"\b([A-Z]{2}\d{6}|\d{8})\b", s, re.I)
    if m:
        return normalize_company_number(m.group(1))
    return None


def extract_quoted_or_name(text: str) -> Optional[str]:
    """Try to get company name from quotes or after 'prospect/client for'."""
    s = text or ""
    m = re.search(r'[“"]([^”"]{2,80})[”"]', s)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:prospect|client|company)\s+(?:for|called|named)\s+(.+?)(?:,|\s+company\s+number|\s+c\.?n\.?|\s+and\s+|\s+with\s+|$)",
        s,
        re.I,
    )
    if m:
        name = m.group(1).strip(" .,")
        if len(name) >= 2:
            return name
    return None
