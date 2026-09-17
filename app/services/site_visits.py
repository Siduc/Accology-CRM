"""Tiny no-cookie visit counter for the public Accology landing."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import SESSION_SECRET
from app.models.site_visit import SiteVisit

logger = logging.getLogger("accountant_crm.site_visits")

_BOT_BITS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "facebookexternalhit",
    "preview",
    "uptime",
    "health",
    "pingdom",
    "python-requests",
    "curl/",
    "wget/",
    "httpie",
    "render/",
)


def _client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        raw = (request.headers.get(header) or "").strip()
        if raw:
            return raw.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host.strip()
    return ""


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return ip in {"127.0.0.1", "::1", "localhost"}


def _ua_kind(ua: str) -> str:
    low = (ua or "").lower()
    if any(bit in low for bit in _BOT_BITS):
        return "bot"
    return "browser"


def _ip_hash(ip: str) -> str:
    raw = f"{(ip or '').strip()}|{(SESSION_SECRET or 'dev')[:24]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def should_count(request: Request) -> bool:
    if request.session.get("user"):
        return False
    method = (request.method or "GET").upper()
    if method not in ("GET", "HEAD"):
        return False
    purpose = (request.headers.get("purpose") or request.headers.get("sec-purpose") or "").lower()
    if "prefetch" in purpose:
        return False
    ua = request.headers.get("user-agent") or ""
    if _ua_kind(ua) == "bot":
        return False
    ip = _client_ip(request)
    if not ip or _is_loopback(ip):
        return False
    return True


def record_visit(db: Session, request: Request, *, path: str = "/") -> None:
    if not should_count(request):
        return
    try:
        ip = _client_ip(request)
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()[:120]
        row = SiteVisit(
            path=(path or "/")[:80],
            host=host,
            ip_hash=_ip_hash(ip),
            ua_kind=_ua_kind(request.headers.get("user-agent") or ""),
        )
        db.add(row)
        db.commit()
    except Exception:
        logger.exception("site_visit_record_failed")
        try:
            db.rollback()
        except Exception:
            pass


def visit_summary(db: Session, *, since_days: int = 14) -> dict[str, Any]:
    now = datetime.utcnow()
    today0 = datetime(now.year, now.month, now.day)
    week0 = today0 - timedelta(days=today0.weekday())
    since = today0 - timedelta(days=max(1, int(since_days)))

    def _hits(since_dt: datetime) -> int:
        return int(
            db.query(func.count(SiteVisit.id))
            .filter(SiteVisit.created_at >= since_dt)
            .scalar()
            or 0
        )

    def _uniq(since_dt: datetime) -> int:
        return int(
            db.query(func.count(func.distinct(SiteVisit.ip_hash)))
            .filter(SiteVisit.created_at >= since_dt)
            .scalar()
            or 0
        )

    return {
        "today": _hits(today0),
        "today_unique": _uniq(today0),
        "week": _hits(week0),
        "week_unique": _uniq(week0),
        "since": _hits(since),
        "since_unique": _uniq(since),
        "since_days": since_days,
    }


def recent_visits(db: Session, *, limit: int = 50) -> list[SiteVisit]:
    return (
        db.query(SiteVisit)
        .order_by(SiteVisit.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
