from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.person import person_clients


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String)
    company_number = Column(String, unique=True, index=True)
    contact_name = Column(String)
    email = Column(String)
    phone = Column(String)
    address_line1 = Column(String)
    address_line2 = Column(String)
    town = Column(String)
    postcode = Column(String)
    client_type = Column(String)
    overall_status = Column(String, default="Active")
    vat_number = Column(String, nullable=True)
    utr = Column(String, nullable=True)
    paye_reference = Column(String, nullable=True)
    accounts_office_reference = Column(String, nullable=True)
    gov_gateway_username = Column(String, nullable=True)  # Government Gateway ID
    gov_gateway_password = Column(String, nullable=True)
    accounts_software_id = Column(String, nullable=True)
    accounts_software_password = Column(String, nullable=True)
    # Kept for older data; UI uses accounts_software_* primarily
    xero_username = Column(String, nullable=True)
    xero_password = Column(String, nullable=True)
    ch_authentication_code = Column(String, nullable=True)  # companies / LLPs
    ch_personal_code = Column(String, nullable=True)  # individuals
    notes = Column(Text)
    source = Column(String, nullable=True, default="manual")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    people = relationship(
        "Person",
        secondary=person_clients,
        back_populates="clients",
        lazy="selectin",
    )
    jobs = relationship("Job", back_populates="client", foreign_keys="Job.client_id")

    def display_name(self) -> str:
        return self.company_name or self.company_number or f"Client #{self.id}"

    def address_block(self) -> str:
        parts = [
            self.address_line1,
            self.address_line2,
            self.town,
            self.postcode,
        ]
        return ", ".join(p for p in parts if p)
