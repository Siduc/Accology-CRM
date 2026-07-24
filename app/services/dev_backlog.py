"""Seed and manage development backlog items for Settings."""

from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from app.models.dev_backlog import DevBacklogItem

# System-seeded items from work started / planned but not finished
SYSTEM_BACKLOG = [
    {
        "title": "Companies House OAuth / Software Filing",
        "detail": (
            "Authorisation flow, token store, and prepare-filing scaffolding are in place. "
            "Paused: public CS electronic submit is not available; tunnel/redirect ops friction; "
            "full filing not live."
        ),
        "status": "paused",
        "area": "Companies House",
        "sort_order": 10,
    },
    {
        "title": "CS pack — full filing path",
        "detail": (
            "Compare CH vs practice, fix actions (address, contacts, accounts dates) done. "
            "Outstanding: one-click CH CS file when API exists; broader bulk refresh."
        ),
        "status": "started",
        "area": "Companies House",
        "sort_order": 20,
    },
    {
        "title": "Production readiness (Render / Postgres)",
        "detail": (
            "Dual SQLite–Postgres path exists. Confirm production env, backups, "
            "SESSION_SECRET, AUTH_*, DATABASE_URL, health checks."
        ),
        "status": "started",
        "area": "Platform",
        "sort_order": 30,
    },
    {
        "title": "Debt chase live mode",
        "detail": "Email/voice/export built; CHASE_LIVE_MODE defaults off. Clean sales ledger before enabling.",
        "status": "started",
        "area": "Sales",
        "sort_order": 40,
    },
    {
        "title": "Asana bi-directional status",
        "detail": "PAT hub, push/pull for Accounts/CS. Deeper field sync and project defaults still thin.",
        "status": "started",
        "area": "Integrations",
        "sort_order": 50,
    },
    {
        "title": "Bank / Purchase / VAT ledgers",
        "detail": "Core modules present. Reconcile polish, VAT edge cases, and reporting depth still open.",
        "status": "started",
        "area": "Finance",
        "sort_order": 60,
    },
    {
        "title": "PWA install polish",
        "detail": "Manifest, icons, offline shell. Push notifications and offline job edit not started.",
        "status": "started",
        "area": "PWA",
        "sort_order": 70,
    },
    {
        "title": "Client Connections (Xero / Sage)",
        "detail": "Opt-in model and Asana wired. Xero/Sage reserved but not connected.",
        "status": "planned",
        "area": "Integrations",
        "sort_order": 80,
    },
    {
        "title": "Practice Groups + Notes",
        "detail": "Groups board and scrap notes live. Further board analytics / note search optional.",
        "status": "started",
        "area": "Practice",
        "sort_order": 90,
    },
    {
        "title": "WIP mobile Live OS view + task ledger",
        "detail": "Horizon tiles refined; task ledger and phone toggles in progress this release.",
        "status": "started",
        "area": "WIP",
        "sort_order": 5,
    },
    {
        "title": "Duplicate / PENDING clients cleanup",
        "detail": "e.g. Access Utilities #185 PENDING vs #1 real CH number — merge or deactivate.",
        "status": "planned",
        "area": "Data",
        "sort_order": 100,
    },
]


def seed_system_backlog(db: Session) -> int:
    """Insert system items if no system rows exist yet (idempotent by title)."""
    existing = {
        (r.title or "")
        for r in db.query(DevBacklogItem)
        .filter(DevBacklogItem.source == "system")
        .all()
    }
    n = 0
    for item in SYSTEM_BACKLOG:
        if item["title"] in existing:
            continue
        db.add(
            DevBacklogItem(
                title=item["title"],
                detail=item.get("detail"),
                status=item.get("status") or "planned",
                source="system",
                area=item.get("area"),
                sort_order=item.get("sort_order") or 100,
            )
        )
        n += 1
    if n:
        db.commit()
    return n


def list_backlog(db: Session, *, include_archived: bool = False) -> List[DevBacklogItem]:
    q = db.query(DevBacklogItem)
    if not include_archived:
        q = q.filter(DevBacklogItem.is_archived.is_(False))
    return q.order_by(DevBacklogItem.sort_order.asc(), DevBacklogItem.id.asc()).all()
