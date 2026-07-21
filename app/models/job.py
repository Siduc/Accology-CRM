from datetime import datetime

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", back_populates="jobs", foreign_keys=[client_id])

    OPEN_STATUSES = ("Planned", "In Progress", "Review", "Filed")
    CLOSED_STATUSES = ("Completed", "Cancelled")
