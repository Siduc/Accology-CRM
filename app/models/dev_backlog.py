"""Development / product backlog items shown in Settings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.database import Base


class DevBacklogItem(Base):
    __tablename__ = "dev_backlog_items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    # planned | started | paused | done
    status = Column(String, default="planned", index=True)
    # system | user
    source = Column(String, default="user")
    area = Column(String, nullable=True)  # e.g. CH, WIP, Sales
    sort_order = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_archived = Column(Boolean, default=False)
