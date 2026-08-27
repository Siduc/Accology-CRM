"""Practice-wide client hold.

Orthogonal to Client.overall_status (Active | Inactive | Prospect | Former).
Inactive = lost (already skipped by the CS board). Hold = we still have the
client, but automating bots must not chase, file, spawn jobs, or raise CS bills.

One hold, one reason. Staff (Simon / CS Bot) set holds with a reason - do not
invent which live clients are bad payers.

Bots MUST call is_held(client) before:
- filing (Companies House CS / accounts)
- code chase (auth / personal code requests)
- spawning jobs (recurring VAT / CS / accounts)
- raising invoices from automation
- debt chase live send (CHASE_LIVE_MODE path)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

HOLD_REASONS = [
    ("bad_payer", "Bad payer / debt"),
    ("no_submissions", "Letting company slide (no filings)"),
    ("disengaging", "Disengaging / notice given"),
    ("client_request", "Client asked us to pause"),
    ("other", "Other"),
]

_REASON_LABELS = {code: label for code, label in HOLD_REASONS}
_TRUTHY = {1, True, "1", "yes"}


def reason_label(reason: Optional[str]) -> str:
    code = (reason or "").strip().lower()
    return _REASON_LABELS.get(code, "")


def is_held(client: Any) -> bool:
    """True when the client is on practice hold. Safe if client is None."""
    if client is None:
        return False
    fn = getattr(client, "is_on_hold", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            pass
    v = getattr(client, "on_hold", 0)
    if v in _TRUTHY:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "yes", "true", "on"):
        return True
    try:
        return int(v) == 1
    except (TypeError, ValueError):
        return False


def set_hold(db: Session, client, *, reason: str, note: str = "", by: str = ""):
    """Set practice hold. Commits. Invalid reason becomes 'other'."""
    code = (reason or "").strip().lower()
    if code not in _REASON_LABELS:
        code = "other"
    client.on_hold = 1
    client.hold_reason = code
    client.hold_note = (note or "").strip() or None
    client.hold_set_at = datetime.utcnow()
    client.hold_set_by = (by or "").strip() or None
    db.commit()
    db.refresh(client)
    return client


def clear_hold(db: Session, client, *, by: str = ""):
    """Clear on_hold. Keeps last reason/note for audit. Commits.

    by is accepted for a future audit trail; hold_set_by is left as who set it.
    """
    client.on_hold = 0
    client.hold_set_at = None
    db.commit()
    db.refresh(client)
    return client


def skip_live_chase(client: Any) -> bool:
    """Gate for CHASE_LIVE_MODE send - skip held clients."""
    return is_held(client)
