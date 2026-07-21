from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, DateTime, UniqueConstraint

from app.database import Base


class ServiceFee(Base):
    """Suggested fee for a service in a given calendar year."""

    __tablename__ = "service_fees"
    __table_args__ = (
        UniqueConstraint("service_code", "year", name="uq_service_fee_code_year"),
    )

    id = Column(Integer, primary_key=True, index=True)
    service_code = Column(String, index=True)  # e.g. Accounts, Confirmation Statement
    service_name = Column(String)  # display label
    year = Column(Integer, index=True)  # fee year, usually period-end year
    fee = Column(Float, default=0.0)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
