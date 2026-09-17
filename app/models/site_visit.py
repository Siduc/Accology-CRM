"""Public landing hits (no cookies). Lives with the serving app's database."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class SiteVisit(Base):
    __tablename__ = "site_visits"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    path = Column(String, default="/")
    host = Column(String, default="", index=True)
    ip_hash = Column(String, default="", index=True)
    ua_kind = Column(String, default="browser")
