"""In-CRM staff notifications (website prospects, future alerts)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base

# Known types — keep free-form String for future events
NOTIFY_TYPES = (
    "website_prospect",
    "prospect",
    "system",
    "job_alert",
    "task_alert",
)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    # website_prospect | prospect | system | …
    type = Column(String, nullable=False, default="system", index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    # Relative CRM path, e.g. /prospecting/12
    link = Column(String, nullable=True)
    # Optional entity refs for dedupe / deep links
    entity_type = Column(String, nullable=True, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    read_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def is_unread(self) -> bool:
        return self.read_at is None
