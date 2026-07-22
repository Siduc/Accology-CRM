"""Practice finance models: bank, purchase (creditors), suppliers."""

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


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="Practice account")
    bank_name = Column(String, nullable=True)
    sort_code = Column(String, nullable=True)
    account_number = Column(String, nullable=True)
    currency = Column(String, default="GBP")
    opening_balance = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    is_primary = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transactions = relationship(
        "BankTransaction",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("bank_accounts.id"), index=True)
    txn_date = Column(Date, default=date.today, index=True)
    description = Column(String)
    # Signed amount: + money in, − money out
    amount = Column(Float, default=0.0)
    reference = Column(String, nullable=True)
    counterparty = Column(String, nullable=True)
    category = Column(String, nullable=True, index=True)
    source = Column(String, nullable=True, default="manual")
    reconciled = Column(Boolean, default=False, index=True)
    reconciled_at = Column(DateTime, nullable=True)
    import_hash = Column(String, nullable=True, index=True)
    matched_type = Column(String, nullable=True, index=True)
    matched_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("BankAccount", back_populates="transactions")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    contact_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address_line1 = Column(String, nullable=True)
    town = Column(String, nullable=True)
    postcode = Column(String, nullable=True)
    vat_number = Column(String, nullable=True)
    payment_terms_days = Column(Integer, default=30)
    default_category = Column(String, default="supplier")
    is_active = Column(Boolean, default=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bills = relationship("CreditorBill", back_populates="supplier")
    payments = relationship("SupplierPayment", back_populates="supplier")

    def display_name(self) -> str:
        return self.name or f"Supplier #{self.id}"


class CreditorBill(Base):
    """Purchase bill / AP document (Working Capital · Creditors)."""

    __tablename__ = "creditor_bills"

    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    # Denormalised / legacy free-text
    supplier_name = Column(String)
    number = Column(String, nullable=True, index=True)
    description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    # Legacy gross field — kept in sync with total
    amount = Column(Float, default=0.0)
    vat_amount = Column(Float, default=0.0)  # legacy alias of vat_total
    issue_date = Column(Date, default=date.today, index=True)
    due_date = Column(Date, nullable=True, index=True)
    subtotal = Column(Float, default=0.0)
    vat_total = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    amount_paid = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    # supplier | vat | other
    category = Column(String, default="supplier")
    # draft | outstanding | part_paid | paid | void
    status = Column(String, default="outstanding", index=True)
    paid_date = Column(Date, nullable=True)
    bank_transaction_id = Column(
        Integer, ForeignKey("bank_transactions.id"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    supplier = relationship("Supplier", back_populates="bills")
    lines = relationship(
        "CreditorBillLine",
        back_populates="bill",
        cascade="all, delete-orphan",
    )


class CreditorBillLine(Base):
    __tablename__ = "creditor_bill_lines"

    id = Column(Integer, primary_key=True, index=True)
    bill_id = Column(Integer, ForeignKey("creditor_bills.id"), index=True)
    description = Column(String)
    qty = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    vat_rate = Column(Float, default=0.0)  # 0.2 = 20%
    line_net = Column(Float, default=0.0)
    line_vat = Column(Float, default=0.0)
    line_total = Column(Float, default=0.0)

    bill = relationship("CreditorBill", back_populates="lines")


class SupplierPayment(Base):
    __tablename__ = "supplier_payments"

    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    payment_date = Column(Date, default=date.today, index=True)
    amount = Column(Float, default=0.0)
    method = Column(String, default="bank")
    reference = Column(String, nullable=True)
    bank_transaction_id = Column(
        Integer, ForeignKey("bank_transactions.id"), nullable=True
    )
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    supplier = relationship("Supplier", back_populates="payments")
    allocations = relationship(
        "SupplierPaymentAllocation",
        back_populates="payment",
        cascade="all, delete-orphan",
    )


class SupplierPaymentAllocation(Base):
    __tablename__ = "supplier_payment_allocations"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("supplier_payments.id"), index=True)
    bill_id = Column(Integer, ForeignKey("creditor_bills.id"), index=True)
    amount = Column(Float, default=0.0)

    payment = relationship("SupplierPayment", back_populates="allocations")
