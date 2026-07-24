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
    # Planned | In Progress | Overdue and Imminent | Planning | Pre Planning | Completed | Cancelled
    status = Column(String, default="Planned", index=True)
    due_on = Column(Date, nullable=True, index=True)
    period_end = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", foreign_keys=[client_id])
    job = relationship("Job", foreign_keys=[job_id])

    CLOSED = ("Completed", "Cancelled")

    def is_closed(self) -> bool:
        return (self.status or "") in self.CLOSED

    def is_overdue(self, today: Optional[date] = None) -> bool:
        if self.is_closed():
            return False
        today = today or date.today()
        return bool(self.due_on and self.due_on < today)

    def display_status(self, today: Optional[date] = None) -> str:
        if self.is_overdue(today) and (self.status or "") not in (
            "Overdue and Imminent",
            "Completed",
            "Cancelled",
        ):
            return "Overdue and Imminent"
        return self.status or "Planned"
