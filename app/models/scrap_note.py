"""Scrapbook / Post-it notes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class ScrapNote(Base):
    __tablename__ = "scrap_notes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    # yellow | pink | blue | green | orange
    color = Column(String, default="yellow")
    pin_live = Column(Boolean, default=False, index=True)
    sort_order = Column(Integer, default=0)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    group_id = Column(Integer, ForeignKey("practice_groups.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
