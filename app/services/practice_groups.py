"""Persisted practice groups: membership, rename, group-wide connections."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import Client, Job
from app.models.practice_group import PracticeGroup, PracticeGroupMember
from app.services.client_connections import (
    CONNECTION_PROVIDERS,
    is_connected,
    set_connection,
)
from app.services.grouping import DEFAULT_GROUP_STATUSES, list_group_summaries


@dataclass
class BoardClient:
    client: Client
    fees: float = 0.0


@dataclass
class BoardGroup:
    group: PracticeGroup
    members: List[BoardClient] = field(default_factory=list)
    total_fees: float = 0.0
    conn_summary: Dict[str, str] = field(default_factory=dict)  # provider -> on|off|mixed


def _client_fees(db: Session, client_ids: Set[int]) -> Dict[int, float]:
    if not client_ids:
        return {}
    rows = (
        db.query(Job.client_id, func.coalesce(func.sum(Job.fee), 0.0))
        .filter(Job.client_id.in_(client_ids))
        .group_by(Job.client_id)
        .all()
    )
    return {int(cid): float(total or 0) for cid, total in rows}


def ensure_seeded(db: Session) -> int:
    """
    If no practice groups exist, seed from people-relationship graph.
    Returns number of groups created.
    """
    existing = db.query(func.count(PracticeGroup.id)).scalar() or 0
    if int(existing) > 0:
        return 0

    summaries = list_group_summaries(db, statuses=DEFAULT_GROUP_STATUSES)
    if not summaries:
        # Fallback: one group per active client optional — skip empty seed
        return 0

    fees_cache: Dict[int, float] = {}
    created = 0
    for s in summaries:
        # Re-fetch component client ids via get detail-like logic
        from app.services.grouping import get_group_detail

        detail = get_group_detail(db, s.group_id)
        if not detail or not detail.clients:
            continue
        g = PracticeGroup(name=s.name or "Group", color="slate")
        db.add(g)
        db.flush()
        for i, c in enumerate(detail.clients):
            # skip if already in another group (unique client)
            if (
                db.query(PracticeGroupMember)
                .filter(PracticeGroupMember.client_id == c.id)
                .first()
            ):
                continue
            db.add(
                PracticeGroupMember(
                    group_id=g.id, client_id=c.id, sort_order=i
                )
            )
        created += 1
    db.commit()
    return created


def count_practice_groups(db: Session) -> int:
    ensure_seeded(db)
    return int(db.query(func.count(PracticeGroup.id)).scalar() or 0)


def create_group(db: Session, name: str, color: str = "slate") -> PracticeGroup:
    ensure_seeded(db)
    g = PracticeGroup(
        name=(name or "New group").strip() or "New group",
        color=(color or "slate").strip() or "slate",
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def rename_group(db: Session, group_id: int, name: str) -> Optional[PracticeGroup]:
    g = db.query(PracticeGroup).filter(PracticeGroup.id == group_id).first()
    if not g:
        return None
    g.name = (name or "").strip() or g.name
    g.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(g)
    return g


def delete_group(db: Session, group_id: int) -> bool:
    g = db.query(PracticeGroup).filter(PracticeGroup.id == group_id).first()
    if not g:
        return False
    db.delete(g)
    db.commit()
    return True


def move_client(
    db: Session, client_id: int, group_id: Optional[int]
) -> bool:
    """Move client into group_id, or ungroup if group_id is None."""
    ensure_seeded(db)
    # remove existing membership
    db.query(PracticeGroupMember).filter(
        PracticeGroupMember.client_id == client_id
    ).delete()
    if group_id is not None:
        g = db.query(PracticeGroup).filter(PracticeGroup.id == group_id).first()
        if not g:
            db.commit()
            return False
        max_ord = (
            db.query(func.coalesce(func.max(PracticeGroupMember.sort_order), 0))
            .filter(PracticeGroupMember.group_id == group_id)
            .scalar()
        )
        db.add(
            PracticeGroupMember(
                group_id=group_id,
                client_id=client_id,
                sort_order=int(max_ord or 0) + 1,
            )
        )
    db.commit()
    return True


def is_group_eligible_client(client: Optional[Client]) -> bool:
    """Live practice clients only — not Lost (Inactive) or disengaged."""
    if not client:
        return False
    st = (client.overall_status or "Active").strip()
    if st == "Inactive":
        return False
    if st not in DEFAULT_GROUP_STATUSES and st not in ("Active", "Former", "Prospect"):
        # treat unknown as live only if not Inactive
        if st.lower() in ("lost", "inactive"):
            return False
    if client.disengagement_date is not None:
        return False
    return st in DEFAULT_GROUP_STATUSES or st in ("Active", "Former", "Prospect", "")


def remove_client_from_groups(db: Session, client_id: int) -> int:
    """Drop a client from all practice groups (e.g. when marked lost)."""
    n = (
        db.query(PracticeGroupMember)
        .filter(PracticeGroupMember.client_id == client_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(n or 0)


def prune_ineligible_members(db: Session) -> int:
    """Remove Inactive / disengaged clients still sitting on the board."""
    members = db.query(PracticeGroupMember).all()
    if not members:
        return 0
    cids = {m.client_id for m in members}
    clients = {
        c.id: c
        for c in db.query(Client).filter(Client.id.in_(cids or {-1})).all()
    }
    removed = 0
    for m in members:
        c = clients.get(m.client_id)
        if not is_group_eligible_client(c):
            db.delete(m)
            removed += 1
    if removed:
        db.commit()
    return removed


def ungrouped_clients(db: Session) -> List[BoardClient]:
    ensure_seeded(db)
    prune_ineligible_members(db)
    member_ids = {
        int(r[0])
        for r in db.query(PracticeGroupMember.client_id).all()
        if r[0]
    }
    q = db.query(Client).filter(
        Client.overall_status.in_(list(DEFAULT_GROUP_STATUSES))
    )
    clients = q.order_by(Client.company_name).all()
    free = [
        c
        for c in clients
        if c.id not in member_ids and is_group_eligible_client(c)
    ]
    fees = _client_fees(db, {c.id for c in free})
    return [BoardClient(client=c, fees=fees.get(c.id, 0.0)) for c in free]


def group_connection_summary(db: Session, group_id: int) -> Dict[str, str]:
    members = (
        db.query(PracticeGroupMember.client_id)
        .filter(PracticeGroupMember.group_id == group_id)
        .all()
    )
    cids = [int(r[0]) for r in members if r[0]]
    out: Dict[str, str] = {}
    if not cids:
        for code, _l, _d in CONNECTION_PROVIDERS:
            out[code] = "off"
        return out
    for code, _l, _d in CONNECTION_PROVIDERS:
        flags = [is_connected(db, cid, code) for cid in cids]
        if all(flags):
            out[code] = "on"
        elif any(flags):
            out[code] = "mixed"
        else:
            out[code] = "off"
    return out


def apply_group_connection(
    db: Session, group_id: int, provider: str, enabled: bool
) -> int:
    members = (
        db.query(PracticeGroupMember)
        .filter(PracticeGroupMember.group_id == group_id)
        .all()
    )
    n = 0
    for m in members:
        set_connection(db, m.client_id, provider, enabled=enabled)
        n += 1
    return n


def list_board(
    db: Session, *, q: Optional[str] = None
) -> tuple[List[BoardGroup], List[BoardClient]]:
    ensure_seeded(db)
    prune_ineligible_members(db)
    groups = (
        db.query(PracticeGroup)
        .options(joinedload(PracticeGroup.members))
        .order_by(PracticeGroup.name.asc())
        .all()
    )
    all_member_cids: Set[int] = set()
    for g in groups:
        for m in g.members:
            all_member_cids.add(m.client_id)
    fees = _client_fees(db, all_member_cids)
    client_map = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_(all_member_cids or {-1}))
        .all()
    }

    needle = (q or "").strip().lower()
    board: List[BoardGroup] = []
    for g in groups:
        members: List[BoardClient] = []
        total = 0.0
        ordered = sorted(g.members, key=lambda m: (m.sort_order or 0, m.id or 0))
        for m in ordered:
            c = client_map.get(m.client_id)
            if not c or not is_group_eligible_client(c):
                continue
            f = fees.get(c.id, 0.0)
            total += f
            members.append(BoardClient(client=c, fees=f))
        # Search: keep group if name or any member matches
        if needle:
            name_hit = needle in (g.name or "").lower()
            mem_hit = any(
                needle in (m.client.display_name() or "").lower()
                or needle in (m.client.company_number or "").lower()
                for m in members
            )
            if not name_hit and not mem_hit:
                continue
        board.append(
            BoardGroup(
                group=g,
                members=members,
                total_fees=round(total, 2),
                conn_summary=group_connection_summary(db, g.id),
            )
        )
    free = ungrouped_clients(db)
    if needle:
        free = [
            m
            for m in free
            if needle in (m.client.display_name() or "").lower()
            or needle in (m.client.company_number or "").lower()
        ]
    return board, free


def get_group_detail(db: Session, group_id: int) -> Optional[BoardGroup]:
    ensure_seeded(db)
    g = (
        db.query(PracticeGroup)
        .options(joinedload(PracticeGroup.members))
        .filter(PracticeGroup.id == group_id)
        .first()
    )
    if not g:
        return None
    board, _ = list_board(db)
    for bg in board:
        if bg.group.id == group_id:
            return bg
    # empty group
    return BoardGroup(
        group=g,
        members=[],
        total_fees=0.0,
        conn_summary=group_connection_summary(db, g.id),
    )
