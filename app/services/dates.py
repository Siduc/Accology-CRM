from datetime import date, timedelta
from typing import Optional, Tuple


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
    elif "Self Assessment" in job_type or job_type == "SA":
        # SA due 31 Jan following the tax year end (5 April)
        statutory = date(period_end.year + 1, 1, 31)
        if period_end.month > 1 or (period_end.month == 1 and period_end.day > 31):
            statutory = date(period_end.year + 1, 1, 31)
        target_start = None
        target_completion = None
    else:
        statutory = period_end + timedelta(days=30)
        target_start = period_end
        target_completion = period_end

    return statutory, target_start, target_completion


JOB_TYPES = [
    "Accounts",
    "Corporation Tax",
    "Confirmation Statement",
    "Self Assessment",
    "VAT Return",
    "Payroll",
    "Bookkeeping",
    "Other",
]

JOB_STATUSES = [
    "Planned",
    "In Progress",
    "Review",
    "Filed",
    "Completed",
    "Cancelled",
]
