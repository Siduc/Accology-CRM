"""Per-client job billing patterns (fee + Done behaviour by job type)."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base

# How Done should bill for this type on this client
PATTERN_ON_DONE = ("default", "draft", "sent", "none")


class ClientJobPattern(Base):
    """
    Client-specific billing for a job type.

    Example: Buzz · VAT Return · fee £0 · on_done=none (covered by retainer).
    Subsequent auto-created jobs pick this up; staff can still type a fee on the job.
    """

    __tablename__ = "client_job_patterns"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "job_type", name="uq_client_job_pattern_type"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    # Canonical Job.type string e.g. "VAT Return", "Accounts"
    job_type = Column(String, nullable=False, index=True)
    # None = use standard schedule / prior-year uplift; 0 = free; >0 = fixed fee
    fee = Column(Float, nullable=True)
    # default | draft | sent | none
    on_done = Column(String, default="default", nullable=False)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", backref="job_patterns")

    def fee_label(self) -> str:
        if self.fee is None:
            return "Standard schedule"
        if float(self.fee or 0) == 0:
            return "£0 (no fee)"
        return f"£{float(self.fee):,.2f}"

    def on_done_label(self) -> str:
        v = (self.on_done or "default").strip().lower()
        return {
            "default": "Standard Done rules",
            "draft": "Draft invoice",
            "sent": "Invoice (sent)",
            "none": "No invoice",
        }.get(v, v)
