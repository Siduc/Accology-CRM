"""Summary stats for lost jobs analysis."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any, Dict, List, Optional

from app.models import Job


def fee_band(fee: float) -> str:
    f = fee or 0.0
    if f < 250:
        return "£0–250"
    if f < 500:
        return "£250–500"
    if f < 1000:
        return "£500–1,000"
    if f < 2000:
        return "£1,000–2,000"
    return "£2,000+"


def analyse_lost_jobs(jobs: List[Job]) -> Dict[str, Any]:
    total_fees = sum(j.fee or 0 for j in jobs)
    count = len(jobs)
    avg = total_fees / count if count else 0.0

    by_type_fees: Dict[str, float] = defaultdict(float)
    by_type_count: Counter = Counter()
    by_year_fees: Dict[int, float] = defaultdict(float)
    by_year_count: Counter = Counter()
    by_band: Counter = Counter()
    by_client_fees: Dict[str, float] = defaultdict(float)
    by_client_count: Counter = Counter()
    late_yes = 0
    late_no = 0
    by_billing: Counter = Counter()
    by_source: Counter = Counter()

    for j in jobs:
        t = j.type or "Other"
        by_type_count[t] += 1
        by_type_fees[t] += j.fee or 0
        if j.period_end:
            y = j.period_end.year
            by_year_count[y] += 1
            by_year_fees[y] += j.fee or 0
        by_band[fee_band(j.fee or 0)] += 1
        cname = (
            j.client.company_name
            if j.client and j.client.company_name
            else (j.client.company_number if j.client else f"#{j.client_id}")
        )
        by_client_count[cname] += 1
        by_client_fees[cname] += j.fee or 0
        if (j.was_late or "").lower() == "yes":
            late_yes += 1
        elif (j.was_late or "").lower() == "no":
            late_no += 1
        if j.billing_status:
            by_billing[j.billing_status] += 1
        if j.source:
            by_source[j.source] += 1

    top_clients = sorted(
        by_client_fees.items(), key=lambda x: x[1], reverse=True
    )[:15]
    type_rows = sorted(
        [
            {"type": t, "count": by_type_count[t], "fees": by_type_fees[t]}
            for t in by_type_count
        ],
        key=lambda r: r["fees"],
        reverse=True,
    )
    year_rows = sorted(
        [
            {"year": y, "count": by_year_count[y], "fees": by_year_fees[y]}
            for y in by_year_count
        ],
        key=lambda r: r["year"],
    )

    return {
        "count": count,
        "total_fees": round(total_fees, 2),
        "average_fee": round(avg, 2),
        "type_rows": type_rows,
        "year_rows": year_rows,
        "fee_bands": [
            {"band": b, "count": by_band[b]}
            for b in ["£0–250", "£250–500", "£500–1,000", "£1,000–2,000", "£2,000+"]
            if by_band[b]
        ],
        "top_clients": [
            {"name": n, "fees": round(f, 2), "count": by_client_count[n]}
            for n, f in top_clients
        ],
        "late_yes": late_yes,
        "late_no": late_no,
        "billing": by_billing.most_common(10),
        "sources": by_source.most_common(10),
    }


def apply_lost_filters(
    jobs: List[Job],
    *,
    job_type: str = "",
    year: str = "",
    fee_band_key: str = "",
    billing: str = "",
    q: str = "",
    late: str = "",
    include_completed: bool = True,
) -> List[Job]:
    out = jobs
    if not include_completed:
        out = [j for j in out if j.status not in ("Completed", "Cancelled")]
    if job_type:
        out = [j for j in out if (j.type or "") == job_type]
    if year:
        try:
            y = int(year)
            out = [j for j in out if j.period_end and j.period_end.year == y]
        except ValueError:
            pass
    if fee_band_key:
        out = [j for j in out if fee_band(j.fee or 0) == fee_band_key]
    if billing:
        out = [j for j in out if (j.billing_status or "") == billing]
    if late:
        out = [j for j in out if (j.was_late or "").lower() == late.lower()]
    if q:
        ql = q.lower()
        out = [
            j
            for j in out
            if j.client
            and (
                ql in (j.client.company_name or "").lower()
                or ql in (j.client.company_number or "").lower()
            )
        ]
    return out
