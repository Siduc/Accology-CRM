from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    ForeignKey,
    Text,
    Table,
    DateTime,
)
from sqlalchemy.orm import relationship

from app.database import Base

client_job = Table(
    "client_job",
    Base.metadata,
    Column("client_id", Integer, ForeignKey("clients.id")),
    Column("job_id", Integer, ForeignKey("jobs.id")),
)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    type = Column(String)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    period_end = Column(Date)
    statutory_due_date = Column(Date)
    target_start = Column(Date)
    target_completion = Column(Date)
    actual_start = Column(Date, nullable=True)
    actual_completion = Column(Date, nullable=True)
    fee = Column(Float, default=0.0)
    status = Column(String, default="Planned")
    is_recurring = Column(String, default="Yes")
    notes = Column(Text)
    # Import / billing / loss analysis (prior job analysis, CH, etc.)
    source = Column(String, nullable=True)  # prior_job_analysis | companies_house | manual
    invoice_reference = Column(String, nullable=True, index=True)
    billing_status = Column(String, nullable=True)
    gross_amount = Column(Float, nullable=True)
    vat_amount = Column(Float, nullable=True)
    was_late = Column(String, nullable=True)  # Yes / No
    lost_reason = Column(String, nullable=True)
    import_key = Column(String, nullable=True, unique=True, index=True)
    # Asana integration
    asana_task_gid = Column(String, nullable=True, index=True)
    asana_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", back_populates="jobs", foreign_keys=[client_id])

    OPEN_STATUSES = (
        "Planned",
        "In Progress",
        "Review",
        "Overdue",
        "Overdue and Imminent",
        "Planning",
        "Pre Planning",
        "Later",
        "Filed",
    )
    CLOSED_STATUSES = ("Completed", "Cancelled")

    def is_closed(self) -> bool:
        return (self.status or "") in self.CLOSED_STATUSES

    def due_date(self):
        """Date used for overdue checks: statutory due, else target completion."""
        return self.statutory_due_date or self.target_completion

    def is_overdue(self, today: Optional[date] = None) -> bool:
        if self.is_closed():
            return False
        today = today or date.today()
        due = self.due_date()
        return bool(due and due < today)

    def display_status(self, today: Optional[date] = None) -> str:
        """
        Status shown in lists from WIP horizon:
        Overdue and Imminent | Planning | Pre Planning | Later
        (workflow status in DB kept unless user edits).
        """
        if self.is_closed():
            return self.status or "—"
        today = today or date.today()
        try:
            from app.services.working_capital import HORIZON_STATUS, job_horizon_key

            key = job_horizon_key(self, today) or "later"
            return HORIZON_STATUS.get(key, self.status or "—")
        except Exception:
            if self.is_overdue(today):
                return "Overdue and Imminent"
            return self.status or "—"
