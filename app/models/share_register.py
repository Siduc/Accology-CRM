"""Client share register — practice-maintained, CH-seeded."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class ShareClass(Base):
    """Share class for a limited company client (e.g. Ordinary £1)."""

    __tablename__ = "share_classes"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    name = Column(String, nullable=False, default="Ordinary")
    currency = Column(String, default="GBP")
    nominal_value = Column(Float, default=1.0)
    # Aggregate issued (from capital statement) when known
    aggregate_shares = Column(Float, nullable=True)
    aggregate_nominal = Column(Float, nullable=True)
    rights_notes = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0)
    source = Column(String, default="manual")  # manual | ch_capital | ch_filing
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", backref="share_classes")
    holdings = relationship(
        "Shareholding",
        back_populates="share_class",
        cascade="all, delete-orphan",
    )


class Shareholding(Base):
    """Member holding — the practice share register row."""

    __tablename__ = "shareholdings"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    share_class_id = Column(Integer, ForeignKey("share_classes.id"), nullable=True, index=True)
    # Link to CRM person when known
    person_id = Column(Integer, ForeignKey("people.id"), nullable=True, index=True)
    member_name = Column(String, nullable=False)
    member_type = Column(String, default="individual")  # individual | corporate
    company_number = Column(String, nullable=True)  # if corporate member
    shares = Column(Float, nullable=True)  # exact count when known
    # PSC nature of control text / band when seeded from CH PSC
    psc_natures = Column(Text, nullable=True)
    certificate_no = Column(String, nullable=True)
    date_acquired = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    # manual | ch_psc | ch_director | ch_both | ch_filing | import
    source = Column(String, default="manual", index=True)
    # Role flags (director + PSC often same person)
    is_director = Column(Boolean, default=False)
    is_psc = Column(Boolean, default=False)
    # draft | verified
    status = Column(String, default="draft", index=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    share_class = relationship("ShareClass", back_populates="holdings")
    client = relationship("Client", backref="shareholdings")
    person = relationship("Person")
