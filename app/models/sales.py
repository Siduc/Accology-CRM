"""Sales Ledger + Services Ledger models."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Service(Base):
    """Services Ledger catalogue entry."""

    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)
    name = Column(String)
    description = Column(Text, nullable=True)
    default_fee = Column(Float, default=0.0)
    default_vat_rate = Column(Float, default=0.0)  # 0.20 = 20%
    unit = Column(String, default="job")  # job | hour | fixed
    category = Column(String, default="compliance")
    is_active = Column(Boolean, default=True)
    is_sellable_to_clients = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    prices = relationship("ServicePrice", back_populates="service", cascade="all, delete-orphan")


class ServicePrice(Base):
    """Optional year-specific price for a service."""

    __tablename__ = "service_prices"
    __table_args__ = (
        UniqueConstraint("service_id", "year", name="uq_service_price_year"),
    )

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"), index=True)
    year = Column(Integer, index=True)
    fee = Column(Float, default=0.0)
    notes = Column(String, nullable=True)

    service = relationship("Service", back_populates="prices")


class Quote(Base):
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(String, unique=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    issue_date = Column(Date, default=date.today)
    valid_until = Column(Date, nullable=True)
    status = Column(String, default="draft", index=True)  # draft sent accepted declined expired
    subtotal = Column(Float, default=0.0)
    vat_total = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    lines = relationship("QuoteLine", back_populates="quote", cascade="all, delete-orphan")


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), index=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    description = Column(String)
    qty = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    vat_rate = Column(Float, default=0.0)
    line_total = Column(Float, default=0.0)

    quote = relationship("Quote", back_populates="lines")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(String, unique=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=True)
    issue_date = Column(Date, default=date.today, index=True)
    due_date = Column(Date, nullable=True, index=True)
    status = Column(String, default="draft", index=True)
    # draft | sent | part_paid | paid | void | written_off
    subtotal = Column(Float, default=0.0)
    vat_total = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    amount_paid = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    source = Column(String, nullable=True)  # job | manual | import | quote
    import_key = Column(String, unique=True, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")
    chase_actions = relationship(
        "DebtChaseAction", back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    description = Column(String)
    qty = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    vat_rate = Column(Float, default=0.0)
    line_total = Column(Float, default=0.0)

    invoice = relationship("Invoice", back_populates="lines")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    payment_date = Column(Date, default=date.today, index=True)
    amount = Column(Float, default=0.0)
    method = Column(String, default="bank")  # bank | cash | card | other
    reference = Column(String, nullable=True)
    bank_transaction_id = Column(
        Integer, ForeignKey("bank_transactions.id"), nullable=True
    )
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    allocations = relationship(
        "PaymentAllocation", back_populates="payment", cascade="all, delete-orphan"
    )


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True)
    amount = Column(Float, default=0.0)

    payment = relationship("Payment", back_populates="allocations")


class DebtChaseAction(Base):
    __tablename__ = "debt_chase_actions"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    action_type = Column(String)  # reminder_1 | email | call | escalate | hold | export
    # polite | firm | final | legal
    stage = Column(String, nullable=True, index=True)
    # email | note | call | export
    channel = Column(String, nullable=True, default="note")
    action_date = Column(Date, default=date.today)
    notes = Column(Text, nullable=True)
    next_action_date = Column(Date, nullable=True)
    email_to = Column(String, nullable=True)
    email_subject = Column(String, nullable=True)
    email_body = Column(Text, nullable=True)
    # preview | dry_run | blocked_not_live | sent | failed | skipped_no_smtp
    send_status = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="chase_actions")
