from datetime import date, datetime, timedelta
from typing import Optional, Tuple
import re

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def uk_date(value, *, empty: str = "") -> str:
    """Format a date as DD/MM/YYYY. Returns *empty* if missing."""
    if value is None or value == "":
        return empty
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return empty or str(value)
    s = str(value).strip()
    if not s:
        return empty
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s[:10]).strftime("%d/%m/%Y")
        if len(s) >= 10 and s[2] == "/" and s[5] == "/":
            return s[:10]
    except Exception:
        pass
    return s


def uk_dates_in_text(text: str) -> str:
    """Rewrite YYYY-MM-DD inside titles/notes as DD/MM/YYYY."""
    if not text:
        return text or ""

    def _repl(m: re.Match) -> str:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        return d.strftime("%d/%m/%Y")

    return _ISO_DATE.sub(_repl, str(text))


def default_period_end(
    job_type: str, today: Optional[date] = None
) -> Optional[date]:
    """
    Sensible default period end when creating a job without an explicit PE.

    Self Assessment → most recent UK tax year end (5 April).
    Accounts / CT → prior calendar year-end (31 Dec) as a planning default.
    Other types → None (caller must supply or leave blank).
    """
    today = today or date.today()
    jt = job_type or ""
    if "Self Assessment" in jt or jt in ("SA", "SAR"):
        # Tax year 6 Apr Y-1 → 5 Apr Y. If before 6 Apr, last complete TY ends prior 5 Apr.
        if today.month < 4 or (today.month == 4 and today.day < 6):
            return date(today.year - 1, 4, 5)
        return date(today.year, 4, 5)
    if "Accounts" in jt or "Corporation Tax" in jt or jt == "CT":
        # Most recent 31 Dec that has fully passed
        if today.month == 12 and today.day == 31:
            return today
        return date(today.year - 1, 12, 31)
    return None


def calculate_dates(
    job_type: str, period_end: Optional[date]
) -> Tuple[Optional[date], Optional[date], Optional[date]]:
    """Return (statutory_due, target_start, target_completion) for a job type."""
    if not period_end:
        return None, None, None

    job_type = job_type or ""

    if "Accounts" in job_type or "Corporation Tax" in job_type or "CT" == job_type:
        statutory = period_end + timedelta(days=274)
        target_start = period_end + timedelta(days=90)
        target_completion = period_end + timedelta(days=120)
    elif "Confirmation" in job_type:
        statutory = period_end + timedelta(days=14)
        target_start = None
        target_completion = None
    elif "Self Assessment" in job_type or job_type in ("SA", "SAR"):
        # UK SA: tax year ends 5 April → online filing deadline 31 January following.
        # e.g. PE 5 Apr 2026 (2025/26) → due 31 Jan 2027
        if period_end.month > 4 or (period_end.month == 4 and period_end.day > 5):
            # After 5 April → belongs to tax year ending next 5 April
            ty_end_year = period_end.year + 1
        else:
            ty_end_year = period_end.year
        statutory = date(ty_end_year + 1, 1, 31)
        # Planning windows for SAR (practice defaults — not CT +90/+120)
        target_start = date(statutory.year - 1, 10, 1)  # from Oct before due
        target_completion = date(statutory.year, 1, 15)  # aim mid-January
    elif "VAT" in job_type:
        # UK VAT: submit and pay 1 calendar month and 7 days after period end
        # (MTD online). Approximate with +37 days then normalise via timedelta.
        from calendar import monthrange

        y, m = period_end.year, period_end.month
        m += 1
        if m > 12:
            m = 1
            y += 1
        last = monthrange(y, m)[1]
        day = min(period_end.day, last)
        month_later = date(y, m, day)
        statutory = month_later + timedelta(days=7)
        target_start = period_end + timedelta(days=1)
        target_completion = statutory - timedelta(days=7)
    else:
        statutory = period_end + timedelta(days=30)
        target_start = period_end
        target_completion = period_end

    return statutory, target_start, target_completion


JOB_TYPES = [
    "Accounts",
    "Self Assessment",
    "Confirmation Statement",
    "Corporation Tax",
    "VAT Return",
    "Payroll",
    "Bookkeeping",
    "Other",
]

JOB_STATUSES = [
    "Planned",
    "In Progress",
    "Review",
    "Today",
    "Tomorrow",
    "This week",
    "On hold",
    "Overdue and Imminent",
    "Planning",
    "Pre Planning",
    "Later",
    "Overdue",
    "Filed",
    "Completed",
    "Cancelled",
]
