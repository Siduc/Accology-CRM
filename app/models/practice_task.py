"""Practice task ledger — standalone or linked to client/job."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class PracticeTask(Base):
    __tablename__ = "practice_tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    fee = Column(Float, default=0.0)
    # Planned | In Progress | On hold | Development | … | Completed | Cancelled
    status = Column(String, default="Planned", index=True)
    due_on = Column(Date, nullable=True, index=True)
    period_end = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    # High | Medium | Low
    priority = Column(String, nullable=True, index=True)
    # Manual ledger order (drag-and-drop); lower = higher on list
    sort_order = Column(Integer, nullable=True, index=True, default=0)
    source_email_date = Column(Date, nullable=True)
    import_source = Column(String, nullable=True)  # e.g. outlook_grok | outlook_push
    import_hash = Column(String, nullable=True, index=True)
    import_batch_id = Column(String, nullable=True, index=True)
    # Selective “push email → task” fields (Outlook / Graph)
    email_from = Column(String, nullable=True)
    email_to = Column(String, nullable=True)
    email_preview = Column(Text, nullable=True)  # short body preview
    # Graph Outlook message linkage (open in Outlook + archive-on-complete)
    outlook_message_id = Column(String, nullable=True, index=True)
    outlook_conversation_id = Column(String, nullable=True)
    outlook_web_link = Column(String, nullable=True)
    outlook_archived_at = Column(DateTime, nullable=True)
    # none | pending | archived | failed
    outlook_archive_status = Column(String, nullable=True, index=True)

    def is_from_email(self) -> bool:
        src = (self.import_source or "").strip().lower()
        if src in ("outlook_push", "outlook_email", "email_push", "outlook_grok"):
            return True
        return bool((self.outlook_message_id or "").strip() or (self.email_from or "").strip())
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", foreign_keys=[client_id])
    job = relationship("Job", foreign_keys=[job_id])

    CLOSED = ("Completed", "Cancelled")
    HOLD = ("On hold",)
    DEVELOPMENT = ("Development",)
    # Not practice WIP (parked thinking / finished)
    INACTIVE = CLOSED + HOLD + DEVELOPMENT
    PRIORITIES = ("High", "Medium", "Low")

    def is_closed(self) -> bool:
        return (self.status or "") in self.CLOSED

    def is_on_hold(self) -> bool:
        return (self.status or "") in self.HOLD

    def is_development(self) -> bool:
        return (self.status or "") in self.DEVELOPMENT

    def is_active(self) -> bool:
        """Open practice work — counts toward WIP task tiles."""
        return (
            not self.is_closed()
            and not self.is_on_hold()
            and not self.is_development()
        )

    def is_overdue(self, today: Optional[date] = None) -> bool:
        if self.is_closed() or self.is_on_hold() or self.is_development():
            return False
        today = today or date.today()
        return bool(self.due_on and self.due_on < today)

    def display_status(self, today: Optional[date] = None) -> str:
        if self.is_on_hold() or self.is_closed() or self.is_development():
            return self.status or "Planned"
        today = today or date.today()
        if self.is_overdue(today):
            return "Overdue"
        # Horizon-style label for due this month / later
        try:
            from app.services.working_capital import (
                HORIZON_STATUS,
                job_horizon_key_for_due,
            )

            key = job_horizon_key_for_due(self.due_on, today)
            if key == "imminent":
                return "Imminent"
            return HORIZON_STATUS.get(key, self.status or "Planned")
        except Exception:
            return self.status or "Planned"

    def display_priority(self) -> str:
        p = (self.priority or "").strip()
        if p in self.PRIORITIES:
            return p
        return "Medium"
