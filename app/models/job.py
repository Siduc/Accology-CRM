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
    # Staff alert (surfaces in top notify banner when due)
    alert_on = Column(Date, nullable=True, index=True)
    alert_note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", back_populates="jobs", foreign_keys=[client_id])

    OPEN_STATUSES = (
        "Planned",
        "In Progress",
        "Review",
        "Today",
        "Tomorrow",
        "This week",
        "Overdue",
        "Overdue and Imminent",
        "Planning",
        "Pre Planning",
        "Later",
        "Filed",
    )
    HOLD_STATUSES = ("On hold",)
    CLOSED_STATUSES = ("Completed", "Cancelled")
    # Not in WIP / active pipeline (held or finished)
    INACTIVE_STATUSES = CLOSED_STATUSES + HOLD_STATUSES

    def is_closed(self) -> bool:
        return (self.status or "") in self.CLOSED_STATUSES

    def is_on_hold(self) -> bool:
        return (self.status or "") in self.HOLD_STATUSES

    def is_active(self) -> bool:
        """Open and not parked — counts toward WIP."""
        return not self.is_closed() and not self.is_on_hold()

    def due_date(self):
        """Date used for overdue checks: statutory due, else target completion."""
        return self.statutory_due_date or self.target_completion

    def is_overdue(self, today: Optional[date] = None) -> bool:
        if self.is_closed() or self.is_on_hold():
            return False
        today = today or date.today()
        due = self.due_date()
        return bool(due and due < today)

    def label(self, *, with_client: bool = False) -> str:
        """Human label for pickers: title · type · period (not raw JOB-id)."""
        title = (self.title or "").strip()
        jtype = (self.type or "").strip()
        pe = ""
        if self.period_end is not None and hasattr(self.period_end, "isoformat"):
            pe = self.period_end.isoformat()
        # Prefer full title when it already carries type
        if title and (not jtype or jtype.lower() in title.lower() or "—" in title):
            core = title
        elif jtype and pe:
            core = f"{jtype} — {pe}"
        elif jtype:
            core = jtype
        elif title:
            core = title
        else:
            core = f"Job {self.id}"
        if with_client and self.client is not None:
            try:
                cname = self.client.display_name()
            except Exception:
                cname = getattr(self.client, "company_name", None) or ""
            if cname:
                return f"{cname} · {core}"
        return core

    def display_status(self, today: Optional[date] = None) -> str:
        """
        Status shown in lists:
        Today | Tomorrow | This week | Overdue | Imminent | Planning | …
        Explicit pins (Today / Tomorrow / This week) win; else due-date rules.
        On hold / closed show as stored.
        """
        if self.is_closed() or self.is_on_hold():
            return self.status or "—"
        st = (self.status or "").strip()
        if st in ("Today", "Tomorrow", "This week"):
            return st
        today = today or date.today()
        try:
            from app.services.working_capital import wip_list_status

            return wip_list_status(self, today)
        except Exception:
            if self.is_overdue(today):
                return "Overdue"
            return self.status or "—"
