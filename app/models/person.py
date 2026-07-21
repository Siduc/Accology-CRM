from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, Table
from sqlalchemy.orm import relationship

from app.database import Base

# Many-to-many: a person can belong to many companies, and a company can have many people.
person_clients = Table(
    "person_clients",
    Base.metadata,
    Column("person_id", Integer, ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
    Column("client_id", Integer, ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True),
    Column("role", String, nullable=True),
    Column("is_primary", Boolean, default=False),
)


class Person(Base):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True, index=True)
    # Legacy single-client FK — kept for old DBs; new links use person_clients.
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    full_name = Column(String)
    email = Column(String)
    phone = Column(String)
    role = Column(String)
    utr = Column(String, nullable=True)
    ni_number = Column(String, nullable=True)
    ch_code = Column(String, nullable=True)
    person_status = Column(String, default="Contact")
    is_primary = Column(Boolean, default=False)
    # True when this person is a client themselves (e.g. SA / tax only, no company)
    is_individual_client = Column(Boolean, default=False)
    notes = Column(Text)

    clients = relationship(
        "Client",
        secondary=person_clients,
        back_populates="people",
        lazy="joined",
    )

    def client_names(self) -> str:
        if not self.clients:
            return ""
        return ", ".join(c.display_name() for c in self.clients)

    def is_linked(self) -> bool:
        return bool(self.clients)

    def company_clients(self):
        """Linked limited companies / firms (excludes Individual client records)."""
        return [
            c
            for c in (self.clients or [])
            if (c.client_type or "").lower() != "individual"
            and not (c.company_number or "").upper().startswith("IND-")
        ]

    def needs_company_link(self) -> bool:
        """Contacts who still look like they should be tied to a company."""
        if self.is_individual_client:
            return False
        return not self.company_clients()
