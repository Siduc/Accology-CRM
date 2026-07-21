"""
Group clients that share people into relationship groups.

A *group* is a connected set of clients: if person P is linked to company A and
company B, A and B belong to the same group. One person with three companies
is one group (not three connections).

Each group is identified by the lowest client id in the set (stable URL key)
and named after the client with the highest total job fees in the group.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Client, Job, Person
from app.models.person import person_clients

# Live practice relationships (matches dashboard Groups tile)
DEFAULT_GROUP_STATUSES = ("Active", "Former", "Prospect")


@dataclass
class ClientGroup:
    """One relationship group (one or more clients sharing people)."""

    client_ids: Set[int] = field(default_factory=set)
    person_ids: Set[int] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.client_ids)

    @property
    def group_id(self) -> int:
        """Stable id for URLs: minimum client primary key in the group."""
        return min(self.client_ids) if self.client_ids else 0


@dataclass
class GroupSummary:
    """Row for the groups list page."""

    group_id: int
    name: str
    lead_client_id: int
    client_count: int
    person_count: int
    total_fees: float
    statuses: List[str] = field(default_factory=list)


@dataclass
class GroupDetail:
    """Full drill-down for one group."""

    group_id: int
    name: str
    lead_client_id: int
    total_fees: float
    clients: List[Client] = field(default_factory=list)
    people: List[Person] = field(default_factory=list)
    client_fees: Dict[int, float] = field(default_factory=dict)


def _person_to_clients(db: Session) -> Dict[int, Set[int]]:
    """Map person_id → set of client_ids (M2M + legacy client_id)."""
    mapping: Dict[int, Set[int]] = defaultdict(set)

    for person_id, client_id in db.query(
        person_clients.c.person_id, person_clients.c.client_id
    ).all():
        if person_id is not None and client_id is not None:
            mapping[int(person_id)].add(int(client_id))

    for person_id, client_id in (
        db.query(Person.id, Person.client_id)
        .filter(Person.client_id.isnot(None))
        .all()
    ):
        mapping[int(person_id)].add(int(client_id))

    return mapping


def _build_client_adjacency(
    person_clients_map: Dict[int, Set[int]],
    allowed_client_ids: Optional[Set[int]] = None,
) -> Dict[int, Set[int]]:
    """Clients are adjacent when they share at least one person."""
    adj: Dict[int, Set[int]] = defaultdict(set)

    def _include(cid: int) -> bool:
        return allowed_client_ids is None or cid in allowed_client_ids

    for _person_id, clients in person_clients_map.items():
        ids = [c for c in clients if _include(c)]
        if not ids:
            continue
        for i, a in enumerate(ids):
            adj[a].add(a)
            for b in ids[i + 1 :]:
                adj[a].add(b)
                adj[b].add(a)

    if allowed_client_ids is not None:
        for cid in allowed_client_ids:
            adj[cid].add(cid)

    return adj


def connected_components(adj: Dict[int, Set[int]]) -> List[Set[int]]:
    """BFS over undirected adjacency → list of client-id sets."""
    seen: Set[int] = set()
    groups: List[Set[int]] = []

    for start in sorted(adj.keys()):
        if start in seen:
            continue
        component: Set[int] = set()
        queue: deque[int] = deque([start])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            component.add(node)
            for nb in adj.get(node, ()):
                if nb not in seen:
                    queue.append(nb)
        if component:
            groups.append(component)

    return groups


def group_clients(
    db: Session,
    *,
    client_ids: Optional[Sequence[int]] = None,
) -> List[ClientGroup]:
    """Compute relationship groups for the given clients (or all clients)."""
    if client_ids is not None:
        allowed = set(int(c) for c in client_ids)
    else:
        allowed = {int(r[0]) for r in db.query(Client.id).all()}

    if not allowed:
        return []

    p2c = _person_to_clients(db)
    adj = _build_client_adjacency(p2c, allowed)
    components = connected_components(adj)

    c2p: Dict[int, Set[int]] = defaultdict(set)
    for pid, cids in p2c.items():
        for cid in cids:
            if cid in allowed:
                c2p[cid].add(pid)

    result: List[ClientGroup] = []
    for comp in components:
        people: Set[int] = set()
        for cid in comp:
            people |= c2p.get(cid, set())
        result.append(ClientGroup(client_ids=set(comp), person_ids=people))

    return result


def count_groups(
    db: Session,
    *,
    client_ids: Optional[Sequence[int]] = None,
) -> int:
    """Number of relationship groups among the given clients."""
    return len(group_clients(db, client_ids=client_ids))


def client_ids_for_period_and_statuses(
    db: Session,
    *,
    statuses: Optional[Iterable[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[int]:
    """Helper: client primary keys filtered by status and created_at window."""
    q = db.query(Client.id)
    if statuses is not None:
        status_list = list(statuses)
        if status_list:
            q = q.filter(Client.overall_status.in_(status_list))
    if start is not None and end is not None:
        q = q.filter(Client.created_at >= start, Client.created_at <= end)
    return [int(r[0]) for r in q.all()]


def _client_fee_totals(db: Session, client_ids: Sequence[int]) -> Dict[int, float]:
    """Sum of job.fee per client_id."""
    if not client_ids:
        return {}
    rows = (
        db.query(Job.client_id, func.coalesce(func.sum(Job.fee), 0.0))
        .filter(Job.client_id.in_(list(client_ids)))
        .group_by(Job.client_id)
        .all()
    )
    return {int(cid): float(total or 0) for cid, total in rows if cid is not None}


def _lead_client(
    clients: Sequence[Client], fees: Dict[int, float]
) -> Tuple[Client, float]:
    """Client with the highest fee total; ties broken by name then id."""
    if not clients:
        raise ValueError("empty clients")

    def key(c: Client):
        return (
            fees.get(c.id, 0.0),
            (c.company_name or "").lower(),
            c.id,
        )

    lead = max(clients, key=key)
    return lead, fees.get(lead.id, 0.0)


def list_group_summaries(
    db: Session,
    *,
    statuses: Sequence[str] = DEFAULT_GROUP_STATUSES,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    q: Optional[str] = None,
) -> List[GroupSummary]:
    """
    Summaries for the groups index: named after the highest-fee client in each group.
    """
    ids = client_ids_for_period_and_statuses(
        db, statuses=statuses, start=start, end=end
    )
    groups = group_clients(db, client_ids=ids)
    if not groups:
        return []

    all_cids = sorted({cid for g in groups for cid in g.client_ids})
    clients = {
        c.id: c
        for c in db.query(Client).filter(Client.id.in_(all_cids)).all()
    }
    fees = _client_fee_totals(db, all_cids)

    summaries: List[GroupSummary] = []
    for g in groups:
        group_clients_list = [clients[cid] for cid in g.client_ids if cid in clients]
        if not group_clients_list:
            continue
        lead, _lead_fee = _lead_client(group_clients_list, fees)
        group_fee = sum(fees.get(cid, 0.0) for cid in g.client_ids)
        statuses_present = sorted(
            {
                (c.overall_status or "—")
                for c in group_clients_list
            }
        )
        name = lead.display_name() if hasattr(lead, "display_name") else (
            lead.company_name or lead.company_number or f"Client #{lead.id}"
        )
        summaries.append(
            GroupSummary(
                group_id=g.group_id,
                name=name,
                lead_client_id=lead.id,
                client_count=len(group_clients_list),
                person_count=len(g.person_ids),
                total_fees=round(group_fee, 2),
                statuses=statuses_present,
            )
        )

    if q:
        needle = q.strip().lower()
        if needle:
            summaries = [s for s in summaries if needle in (s.name or "").lower()]

    # Highest fee groups first, then name
    summaries.sort(key=lambda s: (-s.total_fees, s.name.lower()))
    return summaries


def get_group_detail(
    db: Session,
    group_id: int,
    *,
    statuses: Sequence[str] = DEFAULT_GROUP_STATUSES,
) -> Optional[GroupDetail]:
    """
    Resolve a group by its stable id (min client id) and return full members.
    """
    ids = client_ids_for_period_and_statuses(db, statuses=statuses)
    groups = group_clients(db, client_ids=ids)
    match = next((g for g in groups if g.group_id == group_id), None)
    if not match:
        # Fallback: search among all clients in case group is lost-only
        groups_all = group_clients(db, client_ids=None)
        match = next((g for g in groups_all if g.group_id == group_id), None)
    if not match:
        return None

    clients = (
        db.query(Client)
        .filter(Client.id.in_(list(match.client_ids)))
        .order_by(Client.company_name)
        .all()
    )
    people: List[Person] = []
    if match.person_ids:
        people = (
            db.query(Person)
            .filter(Person.id.in_(list(match.person_ids)))
            .order_by(Person.full_name)
            .all()
        )

    fees = _client_fee_totals(db, list(match.client_ids))
    if clients:
        lead, _ = _lead_client(clients, fees)
        name = lead.display_name()
        lead_id = lead.id
    else:
        name = f"Group #{group_id}"
        lead_id = group_id

    # Sort clients by fee desc for detail table
    clients = sorted(
        clients,
        key=lambda c: (-fees.get(c.id, 0.0), (c.company_name or "").lower()),
    )

    return GroupDetail(
        group_id=match.group_id,
        name=name,
        lead_client_id=lead_id,
        total_fees=round(sum(fees.values()), 2),
        clients=clients,
        people=people,
        client_fees=fees,
    )
