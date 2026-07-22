"""Persisted practice groups (editable membership + names)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class PracticeGroup(Base):
    __tablename__ = "practice_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    notes = Column(Text, nullable=True)
    color = Column(String, nullable=True, default="slate")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = relationship(
        "PracticeGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class PracticeGroupMember(Base):
    __tablename__ = "practice_group_members"
    __table_args__ = (UniqueConstraint("client_id", name="uq_practice_group_client"),)

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("practice_groups.id"), index=True, nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("PracticeGroup", back_populates="members")
