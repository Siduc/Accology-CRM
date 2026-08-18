from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Date, Float, Integer, String, Text, DateTime
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.person import person_clients

RETAINER_FREQUENCIES = ("Monthly", "Quarterly", "Annual")
BILLING_MODELS = ("Per job", "Retainer")

# Client VAT filing scheme (HMRC return frequency)
VAT_FREQUENCIES = ("none", "monthly", "quarterly", "annually")


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
    # Practice book overrides (when set, replace invoice-proxy join/leave dates)
    engagement_date = Column(Date, nullable=True, index=True)
    disengagement_date = Column(Date, nullable=True, index=True)
    vat_number = Column(String, nullable=True)
    # none | monthly | quarterly | annually — this client's HMRC filing scheme
    vat_frequency = Column(String, nullable=True, index=True)
    # stagger_1..4 when quarterly (or annually with quarter-aligned year end)
    vat_quarterly_pattern = Column(String, nullable=True)
    # 1–12: period-end month for annual accounting scheme
    vat_year_end_month = Column(Integer, nullable=True)
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
    # Companies House company auth code (stored encrypted when secrets_crypto available)
    ch_authentication_code = Column(String, nullable=True)  # companies / LLPs
    ch_personal_code = Column(String, nullable=True)  # individuals
    # Share register practice meta
    share_register_verified_at = Column(DateTime, nullable=True)
    share_register_verified_by = Column(String, nullable=True)
    share_register_notes = Column(Text, nullable=True)
    ch_register_seeded_at = Column(DateTime, nullable=True)
    # Billing: per-job fees vs fixed retainer
    billing_model = Column(String, nullable=True, default="Per job")  # Per job | Retainer
    retainer_amount = Column(Float, nullable=True)  # net fee per period
    retainer_frequency = Column(String, nullable=True)  # Monthly | Quarterly | Annual
    retainer_notes = Column(Text, nullable=True)  # what the retainer covers
    notes = Column(Text)
    payroll_onboarding_json = Column(Text, nullable=True)
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
        """Proper-cased name for lists and labels (display only; DB unchanged).

        Individual / IND- shells and names that start with Mr/Mrs/Ms/Miss
        drop honorifics so list sort is by the real name.
        """
        from app.text_format import normalize_caps, normalize_person_name, strip_name_titles

        if not self.company_name:
            return self.company_number or f"Client #{self.id}"

        name = (self.company_name or "").strip()
        ct = (self.client_type or "").strip().lower()
        cn = (self.company_number or "").strip().upper()
        is_person = ct == "individual" or cn.startswith("IND-")
        # Name already carries a title (common on SA person shells)
        titled = bool(name) and strip_name_titles(name) != name
        if is_person or titled:
            return normalize_person_name(name)
        return normalize_caps(name)

    def address_block(self) -> str:
        parts = [
            self.address_line1,
            self.address_line2,
            self.town,
            self.postcode,
        ]
        return ", ".join(p for p in parts if p)

    def is_retainer(self) -> bool:
        """True when billed on a fixed retainer (not pure per-job fees)."""
        model = (self.billing_model or "").strip().lower()
        if model in ("retainer", "fixed", "monthly"):
            return True
        return float(self.retainer_amount or 0) > 0

    def retainer_monthly_net(self) -> float:
        """Normalise retainer to a monthly net figure for dashboards."""
        amt = float(self.retainer_amount or 0)
        if amt <= 0:
            return 0.0
        freq = (self.retainer_frequency or "Monthly").strip().lower()
        if freq.startswith("ann"):
            return round(amt / 12.0, 2)
        if freq.startswith("quart"):
            return round(amt / 3.0, 2)
        return round(amt, 2)

    def retainer_label(self) -> str:
        if not self.is_retainer():
            return ""
        amt = float(self.retainer_amount or 0)
        freq = (self.retainer_frequency or "Monthly").strip() or "Monthly"
        if amt <= 0:
            return f"Retainer ({freq})"
        return f"£{amt:,.0f} {freq.lower()}"

    def vat_scheme_label(self) -> str:
        """Short label for VAT filing scheme + stagger."""
        freq = (self.vat_frequency or "none").strip().lower()
        if freq in ("", "none", "n/a", "na"):
            return "—"
        base = {
            "monthly": "Monthly",
            "quarterly": "Quarterly",
            "annually": "Annually",
            "annual": "Annually",
        }.get(freq, freq.replace("_", " ").title())
        if freq in ("quarterly", "annually", "annual"):
            from app.models.sales import SERVICE_QUARTERLY_PATTERNS

            code = (self.vat_quarterly_pattern or "").strip().lower()
            for key, label, _m in SERVICE_QUARTERLY_PATTERNS:
                if key == code:
                    return f"{base} · {label}"
            if freq in ("annually", "annual") and self.vat_year_end_month:
                months = (
                    "",
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                )
                m = int(self.vat_year_end_month)
                if 1 <= m <= 12:
                    return f"{base} · YE {months[m]}"
        return base
