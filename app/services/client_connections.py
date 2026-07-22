"""Per-client connection flags for external integrations."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.connection import ClientConnection

# code, label, description
CONNECTION_PROVIDERS = (
    (
        "asana",
        "Asana",
        "When enabled, statutory jobs for this client may be pushed to Asana. Default off.",
    ),
    (
        "xero",
        "Xero",
        "Reserved for accounts software link (not wired yet). Stored as an opt-in flag only.",
    ),
    (
        "sage",
        "Sage",
        "Reserved for accounts software link (not wired yet). Stored as an opt-in flag only.",
    ),
)

PROVIDER_CODES = {c for c, _l, _d in CONNECTION_PROVIDERS}


def is_connected(db: Session, client_id: Optional[int], provider: str) -> bool:
    if not client_id:
        return False
    row = (
        db.query(ClientConnection)
        .filter(
            ClientConnection.client_id == client_id,
            ClientConnection.provider == provider,
            ClientConnection.enabled.is_(True),
        )
        .first()
    )
    return row is not None


def get_connection(
    db: Session, client_id: int, provider: str
) -> Optional[ClientConnection]:
    return (
        db.query(ClientConnection)
        .filter(
            ClientConnection.client_id == client_id,
            ClientConnection.provider == provider,
        )
        .first()
    )


def list_connections_for_client(db: Session, client_id: int) -> List[dict]:
    """Merge catalogue with DB rows for UI (missing = disabled)."""
    rows = {
        r.provider: r
        for r in db.query(ClientConnection)
        .filter(ClientConnection.client_id == client_id)
        .all()
    }
    out: List[dict] = []
    for code, label, desc in CONNECTION_PROVIDERS:
        r = rows.get(code)
        out.append(
            {
                "provider": code,
                "label": label,
                "description": desc,
                "enabled": bool(r.enabled) if r else False,
                "external_id": r.external_id if r else None,
                "notes": r.notes if r else None,
            }
        )
    return out


def set_connection(
    db: Session,
    client_id: int,
    provider: str,
    *,
    enabled: bool,
    notes: Optional[str] = None,
    external_id: Optional[str] = None,
) -> ClientConnection:
    if provider not in PROVIDER_CODES:
        raise ValueError(f"Unknown provider: {provider}")
    row = get_connection(db, client_id, provider)
    if not row:
        row = ClientConnection(
            client_id=client_id,
            provider=provider,
            enabled=bool(enabled),
        )
        db.add(row)
    else:
        row.enabled = bool(enabled)
    if notes is not None:
        row.notes = notes or None
    if external_id is not None:
        row.external_id = external_id or None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def save_connection_toggles(
    db: Session, client_id: int, enabled_map: Dict[str, bool]
) -> None:
    """Set enabled flags for known providers (missing keys → disabled)."""
    for code, _label, _desc in CONNECTION_PROVIDERS:
        enabled = bool(enabled_map.get(code, False))
        set_connection(db, client_id, code, enabled=enabled)


def client_ids_with_provider(db: Session, provider: str) -> set:
    rows = (
        db.query(ClientConnection.client_id)
        .filter(
            ClientConnection.provider == provider,
            ClientConnection.enabled.is_(True),
        )
        .all()
    )
    return {int(r[0]) for r in rows if r[0] is not None}
