"""
Job profiles — how to build jobs from Companies House (or other) date sources.

Profiles:
  - Accounts
  - Confirmation Statement

Each profile maps source dates → period_end, statutory due, targets, title, fee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional


@dataclass
class JobDraft:
    """A job ready to create (not yet saved)."""

    type: str
    title: str
    period_end: Optional[date]
    statutory_due_date: Optional[date]
    target_start: Optional[date]
    target_completion: Optional[date]
    fee: float = 0.0
    is_recurring: str = "Yes"
    status: str = "Planned"
    notes: str = ""
    profile_key: str = ""
    source_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobProfile:
    key: str
    label: str
    job_type: str
    default_fee: float
    is_recurring: str
    description: str

    def build_from_companies_house(
        self, ch: Dict[str, Any], company_name: str = ""
    ) -> Optional[JobDraft]:
        raise NotImplementedError


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, type(None)):
        # datetime is subclass of date — normalize
        if hasattr(value, "date") and callable(value.date):
            try:
                return value.date()
            except Exception:
                pass
        return value if type(value) is date else date(value.year, value.month, value.day)
    text = str(value).strip()[:10]
    try:
        y, m, d = text.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


class AccountsProfile(JobProfile):
    def __init__(self, default_fee: float = 0.0):
        super().__init__(
            key="accounts",
            label="Accounts",
            job_type="Accounts",
            default_fee=default_fee,
            is_recurring="Yes",
            description=(
                "Period end = next accounts made up to / period end from Companies House. "
                "Statutory due = CH accounts due date (usually 9 months after period end)."
            ),
        )

    def build_from_companies_house(
        self, ch: Dict[str, Any], company_name: str = ""
    ) -> Optional[JobDraft]:
        accounts = ch.get("accounts") or {}
        next_accounts = accounts.get("next_accounts") or {}

        period_end = _parse_date(
            next_accounts.get("period_end_on")
            or accounts.get("next_made_up_to")
        )
        statutory = _parse_date(
            next_accounts.get("due_on") or accounts.get("next_due")
        )

        if not period_end and not statutory:
            return None

        # If only due date known, still create job
        if period_end and not statutory:
            statutory = period_end + timedelta(days=274)
        if statutory and not period_end:
            # approximate: period end ~ 9 months before due
            period_end = statutory - timedelta(days=274)

        target_start = period_end + timedelta(days=90) if period_end else None
        target_completion = period_end + timedelta(days=120) if period_end else None
        # Don't target past the statutory date
        if target_completion and statutory and target_completion > statutory:
            target_completion = statutory - timedelta(days=14)
        if target_start and statutory and target_start > statutory:
            target_start = statutory - timedelta(days=30)

        overdue = bool(next_accounts.get("overdue") or accounts.get("overdue"))
        pe_label = period_end.isoformat() if period_end else "TBC"
        title = f"Accounts — {pe_label}"
        if company_name:
            title = f"Accounts — {company_name} — {pe_label}"

        notes_parts = ["Created from Companies House company profile."]
        if overdue:
            notes_parts.append("CH flags accounts as OVERDUE.")
        last = (accounts.get("last_accounts") or {}).get("made_up_to")
        if last:
            notes_parts.append(f"Last accounts made up to: {last}.")

        return JobDraft(
            type=self.job_type,
            title=title,
            period_end=period_end,
            statutory_due_date=statutory,
            target_start=target_start,
            target_completion=target_completion,
            fee=self.default_fee,
            is_recurring=self.is_recurring,
            notes=" ".join(notes_parts),
            profile_key=self.key,
            source_fields={
                "period_end_on": next_accounts.get("period_end_on"),
                "due_on": next_accounts.get("due_on") or accounts.get("next_due"),
                "next_made_up_to": accounts.get("next_made_up_to"),
                "overdue": overdue,
            },
        )


class ConfirmationStatementProfile(JobProfile):
    def __init__(self, default_fee: float = 0.0):
        super().__init__(
            key="confirmation_statement",
            label="Confirmation Statement",
            job_type="Confirmation Statement",
            default_fee=default_fee,
            is_recurring="Yes",
            description=(
                "Period end = next confirmation statement made-up-to date from CH. "
                "Statutory due = CH next due (usually 14 days after made-up-to)."
            ),
        )

    def build_from_companies_house(
        self, ch: Dict[str, Any], company_name: str = ""
    ) -> Optional[JobDraft]:
        cs = ch.get("confirmation_statement") or {}
        period_end = _parse_date(cs.get("next_made_up_to"))
        statutory = _parse_date(cs.get("next_due"))

        if not period_end and not statutory:
            return None

        if period_end and not statutory:
            statutory = period_end + timedelta(days=14)
        if statutory and not period_end:
            period_end = statutory - timedelta(days=14)

        # Light internal targets: start a week before made-up-to, complete by due
        target_start = period_end - timedelta(days=7) if period_end else None
        target_completion = statutory

        overdue = bool(cs.get("overdue"))
        pe_label = period_end.isoformat() if period_end else "TBC"
        title = f"Confirmation Statement — {pe_label}"
        if company_name:
            title = f"CS — {company_name} — {pe_label}"

        notes_parts = ["Created from Companies House company profile."]
        if overdue:
            notes_parts.append("CH flags confirmation statement as OVERDUE.")
        if cs.get("last_made_up_to"):
            notes_parts.append(f"Last CS made up to: {cs.get('last_made_up_to')}.")

        return JobDraft(
            type=self.job_type,
            title=title,
            period_end=period_end,
            statutory_due_date=statutory,
            target_start=target_start,
            target_completion=target_completion,
            fee=self.default_fee,
            is_recurring=self.is_recurring,
            notes=" ".join(notes_parts),
            profile_key=self.key,
            source_fields={
                "next_made_up_to": cs.get("next_made_up_to"),
                "next_due": cs.get("next_due"),
                "overdue": overdue,
            },
        )


def default_profiles(
    accounts_fee: float = 0.0, cs_fee: float = 0.0
) -> Dict[str, JobProfile]:
    return {
        "accounts": AccountsProfile(default_fee=accounts_fee),
        "confirmation_statement": ConfirmationStatementProfile(default_fee=cs_fee),
    }


def drafts_from_companies_house(
    ch_profile: Dict[str, Any],
    company_name: str = "",
    profile_keys: Optional[List[str]] = None,
    accounts_fee: float = 0.0,
    cs_fee: float = 0.0,
) -> List[JobDraft]:
    """Build job drafts for selected profiles from a CH company profile JSON."""
    profiles = default_profiles(accounts_fee, cs_fee)
    keys = profile_keys or list(profiles.keys())
    drafts: List[JobDraft] = []
    for key in keys:
        profile = profiles.get(key)
        if not profile:
            continue
        draft = profile.build_from_companies_house(ch_profile, company_name)
        if draft:
            drafts.append(draft)
    return drafts
