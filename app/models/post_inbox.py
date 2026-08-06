"""Scanned post inbox — batches from the practice scanner, review queue, learned rules."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base

# Batch statuses
BATCH_STATUSES = (
    "new",
    "processing",
    "ready",
    "reviewed",
    "error",
)

# Item workflow
ITEM_STATUSES = (
    "inbox",  # needs review
    "suggested",  # rule/AI suggested action
    "filed",
    "emailed",
    "dismissed",
    "error",
)

# Suggested / completed actions
POST_ACTIONS = (
    "review",
    "file_client",
    "file_hmrc",
    "email_client",
    "file_and_email",
    "dismiss",
)

POST_CATEGORIES = (
    "HMRC",
    "Companies House",
    "Bank",
    "Chase / demand",
    "Client correspondence",
    "Supplier",
    "Personal / practice",
    "Other",
)


class PostBatch(Base):
    """One file dropped by the scanner (often multi-document PDF)."""

    __tablename__ = "post_batches"

    id = Column(Integer, primary_key=True, index=True)
    original_filename = Column(String, nullable=False)
    source_path = Column(String, nullable=True)
    content_hash = Column(String, nullable=True, index=True)
    page_count = Column(Integer, default=0)
    size_bytes = Column(Integer, default=0)
    # new | processing | ready | reviewed | error
    status = Column(String, default="new", index=True)
    error_message = Column(Text, nullable=True)
    # Where we parked the original after import
    archived_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)

    items = relationship(
        "PostItem",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="PostItem.sort_order",
    )


class PostItem(Base):
    """One logical document from a batch (after split or whole file)."""

    __tablename__ = "post_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("post_batches.id"), nullable=False, index=True)
    sort_order = Column(Integer, default=0)
    title = Column(String, nullable=True)
    # Page range within parent PDF (1-based inclusive)
    page_start = Column(Integer, nullable=True)
    page_end = Column(Integer, nullable=True)
    # Local path to split PDF / image for preview
    local_path = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, default=0)
    # Extracted text (PDF text layer) for matching / rules
    text_excerpt = Column(Text, nullable=True)
    # Classification
    category = Column(String, nullable=True, index=True)
    suggested_action = Column(String, nullable=True)  # POST_ACTIONS
    suggested_client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    suggested_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    confidence = Column(Float, nullable=True)
    match_reason = Column(String, nullable=True)
    # Review outcome
    status = Column(String, default="inbox", index=True)
    action_taken = Column(String, nullable=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)
    review_notes = Column(Text, nullable=True)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    # Which rule was applied / learned from
    rule_id = Column(Integer, ForeignKey("post_rules.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("PostBatch", back_populates="items")
    suggested_client = relationship("Client", foreign_keys=[suggested_client_id])
    client = relationship("Client", foreign_keys=[client_id])
    job = relationship("Job", foreign_keys=[job_id])
    document = relationship("Document", foreign_keys=[document_id])
    rule = relationship("PostRule", foreign_keys=[rule_id])


class PostRule(Base):
    """Learned / manual routing rules for post (keywords → action)."""

    __tablename__ = "post_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # Comma or newline separated keywords (all or any — see match_mode)
    keywords = Column(Text, nullable=False)
    # all | any
    match_mode = Column(String, default="any")
    # POST_ACTIONS
    action = Column(String, nullable=False, default="review")
    category = Column(String, nullable=True)
    # Optional fixed client for this rule
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    # Priority higher runs first
    priority = Column(Integer, default=100)
    is_active = Column(Boolean, default=True)
    # How many times this rule was confirmed by a human
    hit_count = Column(Integer, default=0)
    learned = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", foreign_keys=[client_id])
