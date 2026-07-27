"""Prospecting Ledger: leads, campaigns, activities, CH sync audit."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Column,
    Date,
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

PIPELINE_STATUSES = (
    "new",
    "contacted",
    "interested",
    "quote_sent",
    "won",
    "lost",
)
PIPELINE_LABELS = {
    "new": "New",
    "contacted": "Contacted",
    "interested": "Interested",
    "quote_sent": "Quote sent",
    "won": "Won",
    "lost": "Lost",
}
OPEN_PIPELINE = ("new", "contacted", "interested", "quote_sent")
ACTIVITY_TYPES = (
    "letter",
    "email",
    "call",
    "social",
    "note",
    "ch_refresh",
    "status_change",
    "convert",
)
CAMPAIGN_CHANNELS = ("letter", "email", "mixed", "call", "social")
CAMPAIGN_STATUSES = ("draft", "active", "paused", "completed")
MEMBER_STATUSES = ("queued", "sent", "responded", "converted", "removed")


class Prospect(Base):
    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, index=True)
    company_number = Column(String, unique=True, index=True, nullable=True)
    pipeline_status = Column(String, default="new", index=True)
    score = Column(Integer, default=0, index=True)
    # Pipeline £ value (quotes / expected fees) — shown as main dashboard metric
    estimated_value = Column(Float, default=0.0)
    source = Column(String, default="manual", index=True)
    contact_name = Column(String, nullable=True)
    email = Column(String, nullable=True, index=True)
    phone = Column(String, nullable=True)
    address_line1 = Column(String, nullable=True)
    address_line2 = Column(String, nullable=True)
    town = Column(String, nullable=True, index=True)
    postcode = Column(String, nullable=True, index=True)
    country = Column(String, nullable=True, default="United Kingdom")
    sic_codes = Column(Text, nullable=True)
    incorporation_date = Column(Date, nullable=True, index=True)
    company_status = Column(String, nullable=True)
    accounts_next_due = Column(Date, nullable=True)
    cs_next_due = Column(Date, nullable=True)
    ch_profile_json = Column(Text, nullable=True)
    ch_officers_json = Column(Text, nullable=True)
    ch_fetched_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, unique=True)
    converted_at = Column(DateTime, nullable=True)
    lost_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    activities = relationship(
        "ProspectActivity",
        back_populates="prospect",
        cascade="all, delete-orphan",
        order_by="desc(ProspectActivity.activity_at)",
    )
    memberships = relationship(
        "CampaignMember",
        back_populates="prospect",
        cascade="all, delete-orphan",
    )

    def display_name(self) -> str:
        return self.company_name or self.company_number or f"Prospect #{self.id}"

    def address_block(self) -> str:
        parts = [self.address_line1, self.address_line2, self.town, self.postcode]
        return ", ".join(p for p in parts if p)

    def pipeline_label(self) -> str:
        return PIPELINE_LABELS.get(self.pipeline_status or "new", self.pipeline_status or "New")

    def is_open(self) -> bool:
        return (self.pipeline_status or "new") in OPEN_PIPELINE


class ProspectCampaign(Base):
    __tablename__ = "prospect_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True, index=True)
    service_label = Column(String, nullable=True)
    channel = Column(String, default="mixed", index=True)
    status = Column(String, default="draft", index=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    sequence_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = relationship(
        "CampaignMember",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    __table_args__ = (
        UniqueConstraint("campaign_id", "prospect_id", name="uq_campaign_prospect"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("prospect_campaigns.id"), index=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), index=True)
    stage = Column(Integer, default=0)
    status = Column(String, default="queued", index=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_touch_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    campaign = relationship("ProspectCampaign", back_populates="members")
    prospect = relationship("Prospect", back_populates="memberships")


class ProspectActivity(Base):
    __tablename__ = "prospect_activities"

    id = Column(Integer, primary_key=True, index=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), index=True, nullable=False)
    campaign_id = Column(Integer, ForeignKey("prospect_campaigns.id"), nullable=True, index=True)
    activity_type = Column(String, default="note", index=True)
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    direction = Column(String, default="outbound")
    outcome = Column(String, nullable=True)
    activity_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_by = Column(String, nullable=True)
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    prospect = relationship("Prospect", back_populates="activities")


class ChSyncRun(Base):
    __tablename__ = "ch_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    kind = Column(String, default="incorporations", index=True)
    params_json = Column(Text, nullable=True)
    created_count = Column(Integer, default=0)
    updated_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    status = Column(String, default="running", index=True)
    message = Column(Text, nullable=True)
