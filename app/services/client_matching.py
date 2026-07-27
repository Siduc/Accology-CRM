"""Shared client name / number / email-domain matching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.client import Client
from app.services.company_numbers import normalize_company_number


@dataclass
class ClientMatch:
    client: Optional[Client] = None
    status: str = "none"  # exact | fuzzy | domain | number | none | ambiguous
    candidates: List[dict] = field(default_factory=list)
    score: float = 0.0


def normalize_client_name(name: str) -> str:
    """Collapse whitespace / punctuation for company matching."""
    s = (name or "").replace("\xa0", " ").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bltd\.?\b", "limited", s)
    s = re.sub(r"\bplc\.?\b", "plc", s)
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _email_domain(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    return e.rsplit("@", 1)[-1].strip()


def _active_clients(db: Session) -> List[Client]:
    return (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive", "Former", "Prospect"]))
        .order_by(Client.company_name.asc())
        .all()
    )


def match_clients_ranked(
    db: Session, name: str, *, limit: int = 5
) -> List[Tuple[Client, float]]:
    """Return up to *limit* (client, score) pairs for a free-text name."""
    raw = (name or "").strip()
    if not raw:
        return []
    want = normalize_client_name(raw)
    if not want:
        return []

    scored: List[Tuple[Client, float]] = []

    # Company number exact
    cn = normalize_company_number(raw)
    if cn:
        c = db.query(Client).filter(Client.company_number == cn).first()
        if c:
            scored.append((c, 100.0))

    # Exact case-insensitive
    c = (
        db.query(Client)
        .filter(func.lower(Client.company_name) == raw.lower())
        .first()
    )
    if c and all(c.id != x.id for x, _ in scored):
        scored.append((c, 95.0))

    tokens = [t for t in want.split() if t not in {"the", "a", "an", "and", "&"}]
    seed = tokens[0] if tokens else want[:6]
    candidates: List[Client] = []
    if len(seed) >= 2:
        candidates = (
            db.query(Client)
            .filter(Client.company_name.ilike(f"%{seed}%"))
            .order_by(Client.id.asc())
            .limit(80)
            .all()
        )
    # Also broader ilike on raw
    more = (
        db.query(Client)
        .filter(Client.company_name.ilike(f"%{raw[:40]}%"))
        .order_by(Client.id.asc())
        .limit(40)
        .all()
    )
    seen_ids = {c.id for c, _ in scored}
    for c in candidates + more:
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        cn_norm = normalize_client_name(c.company_name or "")
        if not cn_norm:
            continue
        score = 0.0
        if cn_norm == want:
            score = 90.0
        elif want in cn_norm or cn_norm in want:
            score = 75.0
        else:
            cn_tokens = [
                t
                for t in cn_norm.split()
                if t not in {"the", "a", "an", "and", "&"}
            ]
            if cn_tokens and tokens:
                overlap = len(set(tokens) & set(cn_tokens))
                need = min(2, len(tokens), len(cn_tokens))
                if overlap >= need:
                    score = 50.0 + overlap * 10.0
        if score > 0:
            scored.append((c, score))

    scored.sort(key=lambda x: (-x[1], (x[0].company_name or "").lower()))
    # de-dupe keep best
    out: List[Tuple[Client, float]] = []
    used = set()
    for c, sc in scored:
        if c.id in used:
            continue
        used.add(c.id)
        out.append((c, sc))
        if len(out) >= limit:
            break
    return out


def match_client_by_email_domain(db: Session, email: str) -> List[Client]:
    domain = _email_domain(email)
    if not domain or domain in (
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "icloud.com",
        "live.com",
        "msn.com",
    ):
        return []
    clients = _active_clients(db)
    hits = []
    for c in clients:
        ce = (c.email or "").strip().lower()
        if ce and _email_domain(ce) == domain:
            hits.append(c)
    return hits


def match_client(
    db: Session,
    *,
    name: str = "",
    email: str = "",
    company_number: str = "",
) -> ClientMatch:
    """
    Resolve a client from free text.
    Status: exact | number | fuzzy | domain | ambiguous | none
    """
    result = ClientMatch()

    # Company number first
    cn = normalize_company_number(company_number or name or "")
    if cn and not (company_number or "").strip():
        # only treat as number if name looks like a number
        if re.fullmatch(r"[A-Za-z]{0,2}\d{6,8}", (name or "").strip().replace(" ", "")):
            pass
        else:
            cn = normalize_company_number(company_number) if company_number else ""
    if company_number:
        cn = normalize_company_number(company_number)
    if cn:
        c = db.query(Client).filter(Client.company_number == cn).first()
        if c:
            result.client = c
            result.status = "number"
            result.score = 100.0
            result.candidates = [
                {"id": c.id, "name": c.display_name(), "score": 100.0}
            ]
            return result

    ranked = match_clients_ranked(db, name, limit=5) if name else []
    if ranked:
        top_c, top_sc = ranked[0]
        result.candidates = [
            {"id": c.id, "name": c.display_name(), "score": sc} for c, sc in ranked
        ]
        # Ambiguous if two close high scores
        if len(ranked) >= 2 and ranked[1][1] >= 70 and abs(ranked[0][1] - ranked[1][1]) < 15:
            result.status = "ambiguous"
            result.score = top_sc
            return result
        result.client = top_c
        result.score = top_sc
        if top_sc >= 90:
            result.status = "exact"
        elif top_sc >= 50:
            result.status = "fuzzy"
        else:
            result.status = "none"
            result.client = None
        if result.client:
            return result

    # Email domain
    domain_hits = match_client_by_email_domain(db, email)
    if len(domain_hits) == 1:
        c = domain_hits[0]
        result.client = c
        result.status = "domain"
        result.score = 60.0
        result.candidates = [{"id": c.id, "name": c.display_name(), "score": 60.0}]
        return result
    if len(domain_hits) > 1:
        result.status = "ambiguous"
        result.candidates = [
            {"id": c.id, "name": c.display_name(), "score": 60.0} for c in domain_hits[:5]
        ]
        return result

    result.status = "none"
    return result
