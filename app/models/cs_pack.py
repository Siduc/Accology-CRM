"""Confirmation Statement review pack (CH download + practice review)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class CsPack(Base):
    """
    Practice CS preparation pack.

    Filled from Companies House public data; filing is completed on WebFiling
    (or Software Filing later) — this is not an electronic CH submission.
    """

    __tablename__ = "cs_packs"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    company_number = Column(String, nullable=True, index=True)
    made_up_to = Column(Date, nullable=True)
    due_on = Column(Date, nullable=True)
    # draft | in_review | ready_to_file | filed | cancelled
    status = Column(String, default="draft", index=True)
    ch_snapshot_json = Column(Text, nullable=True)
    form_json = Column(Text, nullable=True)
    review_notes = Column(Text, nullable=True)
    confirmed_no_changes = Column(String, nullable=True)  # yes | no | partial
    prepared_by = Column(String, nullable=True)
    ready_at = Column(DateTime, nullable=True)
    filed_at = Column(DateTime, nullable=True)
    # API Filing prep (OAuth / transactions — not full CS01 submit yet)
    ch_transaction_id = Column(String, nullable=True)
    filing_prep_json = Column(Text, nullable=True)
    oauth_token_id = Column(Integer, ForeignKey("ch_oauth_tokens.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
