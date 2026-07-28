"""Structured practice email log + templates (not an Outlook mirror)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, default="Other", index=True)
    subject_template = Column(String, nullable=False, default="")
    body_template = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmailMessage(Base):
    """Logged correspondence against a client (± job)."""

    __tablename__ = "email_messages"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    template_id = Column(
        Integer, ForeignKey("email_templates.id"), nullable=True, index=True
    )
    # outbound | inbound
    direction = Column(String, default="outbound", index=True)
    to_address = Column(String, nullable=True)
    cc_address = Column(String, nullable=True)
    subject = Column(String, nullable=False, default="")
    body = Column(Text, nullable=True)
    # draft | sent | failed | logged | blocked_not_live | skipped_no_smtp
    status = Column(String, default="logged", index=True)
    provider = Column(String, nullable=True)  # graph | smtp
    graph_message_id = Column(String, nullable=True, index=True)
    internet_message_id = Column(String, nullable=True)
    conversation_id = Column(String, nullable=True, index=True)
    error_detail = Column(Text, nullable=True)
    sent_by = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", foreign_keys=[client_id])
    job = relationship("Job", foreign_keys=[job_id])
    template = relationship("EmailTemplate", foreign_keys=[template_id])
