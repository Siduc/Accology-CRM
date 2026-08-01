"""
Demo / presentation mode — anonymise confidential data in the UI only.

Session flag `demo_mode` (bool). Real database is never changed.
Stable pseudonyms so the same client/person looks consistent while navigating.

Money: always show polished demo fees (never the live book):
  Accounts £10,000 · Confirmation Statement £100 · Self Assessment £1,000
  Tasks variable £250–£2,500 · band/type totals = count × unit fee.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

SESSION_KEY = "demo_mode"
# When True, user logged in with demo credentials and cannot exit to live data
SESSION_LOCKED_KEY = "demo_locked"

# Identity / contact fields (NOT structural UI labels — those stay readable)
_NAME_KEYS = frozenset(
    {
        "company_name",
        "full_name",
        "contact_name",
        "name",
        "title",
        "display_name",
        "client_name",
        "person_name",
        "supplier_name",
        "account_name",
        "practice_name",
        "ch_name",
    }
)
# "label" is often a WIP band / service type — only anonymise when it looks like a name
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_STRUCTURAL_LABEL_RE = re.compile(
    r"^(?:"
    r"today|later|all|total|other|tasks?|vat|"
    r"accounts|self assessment|confirmation statements?|compliance|"
    r"overdue(?: and imminent)?|imminent|planning|pre planning|everything else|"
    r"m[1-4]|pe[_\s]?\d{4}(?:\s*prior)?|"
    r"(?:" + "|".join(_MONTH_NAMES) + r")(?:\s+\d{4})?"
    r")$",
    re.I,
)
_ADDRESS_KEYS = frozenset(
    {
        "address_line1",
        "address_line2",
        "address",
        "registered_office",
        "town",
        "locality",
        "postcode",
        "postal_code",
        "country",
    }
)
_CONTACT_KEYS = frozenset(
    {
        "email",
        "email_to",
        "phone",
        "mobile",
    }
)
_SECRET_KEYS = frozenset(
    {
        "password",
        "gov_gateway_password",
        "accounts_software_password",
        "xero_password",
        "ch_authentication_code",
        "ch_personal_code",
        "ch_code",
        "gov_gateway_username",
        "accounts_software_id",
        "xero_username",
        "utr",
        "ni_number",
        "vat_number",
        "paye_reference",
        "accounts_office_reference",
        "company_number",
        "sort_code",
        "account_number",
        "auth_code",
        "api_key",
        "token",
        "secret",
    }
)
_MONEY_HINTS = (
    "fee",
    "amount",
    "total",
    "balance",
    "value",
    "subtotal",
    "vat",
    "gross",
    "net",
    "price",
    "paid",
    "owing",
    "debt",
    "wip",
    "cash",
    "retainer",
    "pipeline",
    "monthly",
    "annual",
)

# Well-known companies for polished demos (stable pick by hash)
_DEMO_COMPANIES = (
    "Apple",
    "NVIDIA",
    "Tesla",
    "Microsoft",
    "Amazon",
    "Google",
    "Meta",
    "Netflix",
    "Spotify",
    "Adobe",
    "Salesforce",
    "Oracle",
    "IBM",
    "Intel",
    "Samsung",
    "Sony",
    "Disney",
    "Boeing",
    "Airbus",
    "Shell",
    "BP",
    "Unilever",
    "Nike",
    "Adidas",
    "Starbucks",
    "McDonald's",
    "Coca-Cola",
    "PepsiCo",
    "Walmart",
    "Costco",
    "Uber",
    "Airbnb",
    "OpenAI",
    "SpaceX",
    "Stripe",
    "Shopify",
    "PayPal",
    "Visa",
    "Mastercard",
    "JPMorgan",
    "Goldman Sachs",
    "Barclays",
    "HSBC",
    "Rolls-Royce",
    "Bae Systems",
    "AstraZeneca",
    "Pfizer",
    "Lego",
    "IKEA",
    "Ferrari",
)

# Film & music stars for people demos (full names; stable pick by hash)
_DEMO_PEOPLE = (
    "Taylor Swift",
    "Beyoncé Knowles",
    "Ed Sheeran",
    "Adele Adkins",
    "Drake Graham",
    "Rihanna Fenty",
    "Bruno Mars",
    "Billie Eilish",
    "Harry Styles",
    "Dua Lipa",
    "The Weeknd",
    "Lady Gaga",
    "Elton John",
    "Paul McCartney",
    "Stevie Wonder",
    "Tom Hanks",
    "Meryl Streep",
    "Leonardo DiCaprio",
    "Scarlett Johansson",
    "Denzel Washington",
    "Cate Blanchett",
    "Brad Pitt",
    "Angelina Jolie",
    "Morgan Freeman",
    "Emma Stone",
    "Timothée Chalamet",
    "Zendaya Coleman",
    "Margot Robbie",
    "Ryan Gosling",
    "Florence Pugh",
    "Idris Elba",
    "Hugh Jackman",
    "Keanu Reeves",
    "Viola Davis",
    "Pedro Pascal",
    "Austin Butler",
    "Sydney Sweeney",
    "Cillian Murphy",
    "Christopher Nolan",
    "Steven Spielberg",
)


def is_demo_request(request) -> bool:
    try:
        # Locked demo login always forces demo mode
        if request.session.get(SESSION_LOCKED_KEY):
            return True
        return bool(request.session.get(SESSION_KEY))
    except Exception:
        return False


def is_demo_locked(request) -> bool:
    """True when visitor logged in with demo-only credentials (cannot go live)."""
    try:
        return bool(request.session.get(SESSION_LOCKED_KEY))
    except Exception:
        return False


def set_demo_mode(request, enabled: bool) -> None:
    """Toggle demo for staff sessions. No-op exit when demo is locked."""
    if not enabled and is_demo_locked(request):
        return
    request.session[SESSION_KEY] = bool(enabled)
    if not enabled:
        request.session.pop(SESSION_LOCKED_KEY, None)


def enter_demo_locked(request, *, username: str = "demo") -> None:
    """Log visitor into demo-only mode (cannot switch to live)."""
    request.session.clear()
    request.session["user"] = username
    request.session[SESSION_KEY] = True
    request.session[SESSION_LOCKED_KEY] = True


def _h(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:12], 16)


def demo_company_name(key: Any, original: Any = None) -> str:
    n = _h(f"co:{key}:{original or ''}")
    return _DEMO_COMPANIES[n % len(_DEMO_COMPANIES)]


def demo_person_name(key: Any, original: Any = None) -> str:
    n = _h(f"pe:{key}:{original or ''}")
    return _DEMO_PEOPLE[n % len(_DEMO_PEOPLE)]


def demo_email(key: Any) -> str:
    n = _h(f"em:{key}") % 9000 + 1000
    # Stable fake corporate-style address (not a real inbox)
    person = demo_person_name(key).lower().replace(" ", ".").replace("'", "")
    company = demo_company_name(f"emco:{key}").lower().replace(" ", "").replace("'", "")
    return f"{person.split('.')[0]}.{n}@{company}.example"


def demo_phone(key: Any) -> str:
    n = _h(f"ph:{key}") % 10_000_000
    return f"07{n:09d}"[:11]


def demo_company_number(key: Any) -> str:
    n = _h(f"cn:{key}") % 90_000_000 + 10_000_000
    return f"{n:08d}"


# Standard demo fees by service (presentation only)
DEMO_FEE_ACCOUNTS = 10_000.0
DEMO_FEE_CS = 100.0  # Confirmation Statement / compliance statement
DEMO_FEE_SA = 1_000.0  # Self Assessment
DEMO_FEE_VAT = 500.0
DEMO_FEE_OTHER_JOB = 750.0
# Tasks: variable set (stable per seed)
_DEMO_TASK_FEES = (250.0, 350.0, 500.0, 750.0, 1_000.0, 1_250.0, 1_500.0, 2_000.0, 2_500.0)


def _as_float(val: Any) -> float:
    """Coerce int/float/Decimal/str money to float; unknown → 0."""
    if val is None or val is False:
        return 0.0
    if isinstance(val, bool):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _as_count(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _is_numeric(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    return isinstance(val, (int, float, Decimal))


def demo_fee_for_service(job_type: Any = "", *, key: Any = "") -> float:
    """Accounts £10k · CS £100 · SA £1k · VAT £500 · other jobs £750."""
    parts = [str(x).strip().lower() for x in (job_type, key) if x is not None and str(x).strip()]
    t = " ".join(parts).strip()
    tokens = set(parts)
    # Calendar / ageing band keys are not services — use generic WIP unit
    band_tokens = {
        "today",
        "m1",
        "m2",
        "m3",
        "later",
        "all",
        "total",
        "imminent",
        "planning",
        "pre planning",
        "everything else",
        "overdue and imminent",
        "overdue",
    }
    if tokens and tokens <= band_tokens:
        return 4_000.0
    if t in band_tokens or (t and all(p in band_tokens or p in _MONTH_NAMES for p in parts)):
        if "confirmation" not in t and "accounts" not in t and "self assessment" not in t:
            return 4_000.0
    if any(m in t for m in _MONTH_NAMES) and "confirmation" not in t and "accounts" not in t:
        if "self assessment" not in t:
            return 4_000.0
    if "confirmation" in t or t in ("cs", "compliance statement", "compliance") or tokens & {
        "cs",
        "compliance",
    }:
        return DEMO_FEE_CS
    if "self assessment" in t or t in ("sa", "sar") or tokens & {"sa", "sar"}:
        return DEMO_FEE_SA
    if "accounts" in t:
        return DEMO_FEE_ACCOUNTS
    if "vat" in t:
        return DEMO_FEE_VAT
    if "task" in t:
        return demo_task_fee(key or job_type or "task")
    if "retainer" in t:
        return 1_500.0
    return DEMO_FEE_OTHER_JOB


def demo_task_fee(seed: Any = "") -> float:
    """Variable task fees for demos (stable per seed)."""
    n = _h(f"taskfee:{seed}")
    return float(_DEMO_TASK_FEES[n % len(_DEMO_TASK_FEES)])


def _unit_fee_for_band_label(label: Any) -> float:
    return demo_fee_for_service(label, key=label)


def _is_structural_label(text: Any) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if _STRUCTURAL_LABEL_RE.match(s):
        return True
    # Month Year e.g. "August 2026"
    low = s.lower()
    for m in _MONTH_NAMES:
        if low.startswith(m) and any(ch.isdigit() for ch in s):
            return True
    # Service-like phrases
    if any(
        p in low
        for p in (
            "accounts",
            "self assessment",
            "confirmation",
            "overdue",
            "planning",
            "everything else",
            "work in progress",
            "debtor",
            "creditor",
            "pipeline",
        )
    ):
        return True
    return False


def demo_money(
    val: Any,
    seed: Any = "",
    *,
    job_type: Any = "",
    is_task: bool = False,
    band_key: Any = "",
    count: Any = None,
    context_key: str = "",
) -> float:
    """
    Presentation fees (never live figures):
      Accounts £10,000 · Confirmation Statement £100 · Self Assessment £1,000
      Tasks: variable (£250–£2,500) · aggregates = count × unit when count known.
    Zero live fees still become demo fees when a service/type/count is known.
    """
    n = _as_count(count)
    ck = (context_key or "").lower()
    jt = str(job_type or "").strip()
    bk = str(band_key or "").strip()

    # Task fees / task totals
    if is_task or ("task" in ck and "count" not in ck and not jt):
        if n is not None:
            return round(n * 750.0, 2) if n > 0 else 0.0
        # Single task row or unknown count — still show a demo fee (not blank)
        return demo_task_fee(seed or ck or "task")

    # Typed job / type-tile / band with known service
    if jt or (bk and not _is_band_only_key(bk)):
        unit = demo_fee_for_service(jt, key=bk or jt)
        if n is not None:
            return round(float(n) * unit, 2) if n > 0 else 0.0
        return unit

    # Calendar / ageing band: count × generic WIP unit
    if bk or n is not None:
        unit = _unit_fee_for_band_label(bk or jt or ck)
        if n is not None:
            return round(float(n) * unit, 2) if n > 0 else 0.0
        # No count — if original was non-zero, map to polished bucket; if zero, use unit
        v = _as_float(val)
        if v == 0:
            return unit
        return _bucket_money(v, seed)

    # Aggregates without type/count: polished buckets from magnitude
    v = _as_float(val)
    if v == 0:
        # Truly empty (no count, no type, zero) — leave 0
        return 0.0
    return _bucket_money(v, seed)


def _is_band_only_key(key: Any) -> bool:
    s = str(key or "").strip().lower()
    return s in (
        "today",
        "m1",
        "m2",
        "m3",
        "later",
        "all",
        "total",
        "imminent",
        "planning",
        "pre planning",
        "everything else",
        "overdue and imminent",
    ) or any(s.startswith(m) for m in _MONTH_NAMES)


def _bucket_money(v: float, seed: Any = "") -> float:
    h = _h(f"agg:{seed}:{round(abs(v), 0)}")
    buckets = (
        5_000,
        10_000,
        15_000,
        25_000,
        50_000,
        75_000,
        100_000,
        150_000,
        250_000,
        500_000,
    )
    mag = abs(v)
    best = min(
        buckets,
        key=lambda b: abs(math.log10(max(b, 1)) - math.log10(max(mag, 1))),
    )
    nudge = buckets[h % len(buckets)]
    out = float(best if abs(best - mag) < abs(nudge - mag) else nudge)
    return out if v >= 0 else -out


def _is_money_key(key: str) -> bool:
    k = (key or "").lower().replace("-", "_")
    # Never treat pure counts / ids as money
    if k in (
        "id",
        "count",
        "year",
        "period",
        "status",
        "key",
        "pct",
        "n",
        "length",
        "open_n",
        "email_n",
        "overdue_n",
        "unlinked_n",
        "open_count",
        "filter_count",
        "tasks_count",
        "retainer_count",
        "jobs_count",
        "total_groups",
        "total_clients",
        "total_new_clients",
        "total_lost",
        "total_prospects",
        "prospecting_open",
        "people_count",
        "individual_clients",
        "company_clients",
        "active_clients",
        "opening_clients",
        "closing_clients",
        "total_count",
        "total_groups",
    ):
        return False
    if k.endswith("_count") or k.endswith("_n") or k.startswith("count_"):
        return False
    if k in (
        "total",
        "value",
        "fee",
        "amount",
        "balance",
        "subtotal",
        "total_amount",
        "total_value",
        "expected",
        "total_expected",
        "gross_amount",
        "net_amount",
    ):
        return True
    return any(h in k for h in _MONEY_HINTS)


def _mask_secret(val: Any) -> str:
    if val is None or val == "" or val == "—":
        return "—" if val == "—" else ""
    return "••••••••"


def _seed_for(obj: Any, attr: str) -> str:
    oid = getattr(obj, "id", None)
    if oid is not None:
        return f"{type(obj).__name__}:{oid}:{attr}"
    return f"{type(obj).__name__}:{attr}:{id(obj)}"


class DemoProxy:
    """Attribute proxy that anonymises sensitive fields on model-like objects."""

    __slots__ = ("_obj",)

    def __init__(self, obj: Any):
        object.__setattr__(self, "_obj", obj)

    def _raw(self) -> Any:
        return object.__getattribute__(self, "_obj")

    def __repr__(self) -> str:
        return f"<Demo {type(self._raw()).__name__}>"

    def __bool__(self) -> bool:
        return bool(self._raw())

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, DemoProxy):
            return self._raw() is other._raw() or self._raw() == other._raw()
        return self._raw() == other

    def __hash__(self) -> int:
        try:
            return hash(self._raw())
        except TypeError:
            return id(self._raw())

    def __getattr__(self, name: str) -> Any:
        obj = self._raw()
        # Special methods used in templates
        if name == "display_name":
            def _display_name() -> str:
                return demo_company_name(
                    getattr(obj, "id", 0), getattr(obj, "company_name", None)
                )

            return _display_name
        if name == "address_block":
            def _address_block() -> str:
                return "1 Demo Street, Sample Town, XX1 1XX"

            return _address_block
        if name == "client_names":
            def _client_names() -> str:
                clients = getattr(obj, "clients", None) or []
                parts = []
                for c in clients:
                    parts.append(
                        demo_company_name(getattr(c, "id", 0), getattr(c, "company_name", None))
                    )
                return ", ".join(parts)

            return _client_names
        if name == "company_clients":
            def _company_clients():
                raw = obj.company_clients() if callable(getattr(obj, "company_clients", None)) else []
                return [DemoProxy(c) for c in (raw or [])]

            return _company_clients

        try:
            val = getattr(obj, name)
        except AttributeError:
            raise AttributeError(name) from None

        if callable(val) and not isinstance(val, type):
            # Leave methods (except handled above) — may return sensitive data;
            # wrap bound methods that look like property-style rarely used
            return val

        return _mask_attr(name, obj, val)

    def __getitem__(self, item: Any) -> Any:
        obj = self._raw()
        if hasattr(obj, "__getitem__"):
            val = obj[item]
            if isinstance(item, str):
                return _mask_attr(item, obj, val)
            return anonymize_value(val)
        raise TypeError(f"{type(obj).__name__} is not subscriptable")


def _mask_attr(name: str, obj: Any, val: Any) -> Any:
    key = (name or "").strip()
    kl = key.lower()
    seed = _seed_for(obj, key)

    if kl in _SECRET_KEYS or "password" in kl or kl.endswith("_code") or "secret" in kl:
        if isinstance(val, str) or val is None:
            return _mask_secret(val)
        return val

    if kl in _CONTACT_KEYS or kl.endswith("email"):
        if not val:
            return val or ""
        return demo_email(seed)

    if kl in ("phone", "mobile") or kl.endswith("phone"):
        if not val:
            return val or ""
        return demo_phone(seed)

    if kl in _ADDRESS_KEYS:
        if not val:
            return val or ""
        if "post" in kl or "postal" in kl:
            return "XX1 1XX"
        if "town" in kl or "locality" in kl:
            return "Sample Town"
        if "country" in kl:
            return "United Kingdom"
        return "1 Demo Street"

    if kl == "company_number" or kl.endswith("company_number"):
        if not val:
            return val or ""
        s = str(val)
        if s.upper().startswith("IND-"):
            return f"IND-DEMO{(_h(seed) % 10000):04d}"
        return demo_company_number(seed)

    if kl == "label" or (kl == "title" and _is_structural_label(val)):
        # WIP band / service / month labels must stay readable in demo
        if _is_structural_label(val):
            return val
        # Non-structural label → treat as company-ish name
        if not val or val == "—":
            return val
        return demo_company_name(seed, str(val))

    if kl in _NAME_KEYS or kl.endswith("_name") or kl == "title":
        if not val or val == "—":
            return val
        s = str(val)
        if kl == "title" and any(
            w in s for w in ("Accounts", "Confirmation", "Self Assessment", "VAT", "Job")
        ):
            # Anonymise company name segment if present
            return re.sub(
                r"[A-Za-z][A-Za-z0-9&'’.\- ]{2,40}(?:Ltd|Limited|LLP)?",
                lambda m: demo_company_name(seed + m.group(0), m.group(0))
                if len(m.group(0)) > 4
                else m.group(0),
                s,
                count=1,
            )
        if kl in ("full_name", "contact_name", "person_name") or "person" in kl:
            return demo_person_name(seed, s)
        if kl in ("company_name", "client_name", "supplier_name", "account_name"):
            return demo_company_name(seed, s)
        if kl in ("name", "display_name", "practice_name", "ch_name"):
            if "," in s or s.isupper():
                return demo_person_name(seed, s)
            return demo_company_name(seed, s)
        return demo_company_name(seed, s)

    if kl in ("notes", "body", "detail", "description", "message", "subject") and isinstance(
        val, str
    ):
        if not val.strip():
            return val
        # Keep short structural details (band subtitles)
        if _is_structural_label(val) or len(val) < 80 and any(
            w in val.lower()
            for w in ("deadline", "overdue", "imminent", "status today", "undated", "from ")
        ):
            return val
        return "[Anonymised in demo mode]"

    if _is_numeric(val) and _is_money_key(kl):
        return _mask_money_attr(kl, obj, val, seed)

    return anonymize_value(val)


def _obj_is_task(obj: Any) -> bool:
    name = type(obj).__name__
    if name == "PracticeTask":
        return True
    try:
        from app.models.practice_task import PracticeTask as _PT

        if isinstance(obj, _PT):
            return True
    except Exception:
        pass
    return False


def _mask_money_attr(kl: str, obj: Any, val: Any, seed: str) -> float:
    """Map any money attribute to the demo fee schedule."""
    jtype = (
        getattr(obj, "type", None)
        or getattr(obj, "job_type", None)
        or ""
    )
    is_task = _obj_is_task(obj)
    band_key = getattr(obj, "key", None) or getattr(obj, "label", None) or ""
    # Prefer explicit count fields used by WIP snapshots / tiles
    count = getattr(obj, "count", None)
    if count is None and hasattr(obj, "total_count"):
        count = getattr(obj, "total_count", None)
    if count is None and kl in ("tasks_value",) and hasattr(obj, "tasks_count"):
        # Only tasks_count — do NOT fall back to job count
        count = getattr(obj, "tasks_count", 0) or 0

    if is_task and kl in ("fee", "amount", "value", "gross_amount"):
        return demo_task_fee(seed)

    if jtype and kl in ("fee", "gross_amount", "amount", "value", "total_amount"):
        unit = demo_fee_for_service(jtype)
        n = _as_count(count)
        if n is not None and kl in ("amount", "value", "total_amount", "total"):
            return round(float(n) * unit, 2) if n > 0 else 0.0
        return unit

    if kl == "tasks_value":
        n = _as_count(getattr(obj, "tasks_count", None))
        if n is None:
            n = 0
        return round(float(n) * 750.0, 2) if n > 0 else 0.0

    if kl == "jobs_value":
        n = _as_count(getattr(obj, "count", None)) or 0
        # Blended book average (accounts-heavy demo)
        return round(float(n) * 5_000.0, 2) if n > 0 else 0.0

    if kl in ("value", "total") and (
        hasattr(obj, "jobs_value") or hasattr(obj, "tasks_value")
    ):
        jv = _mask_money_attr(
            "jobs_value", obj, getattr(obj, "jobs_value", 0) or 0, f"{seed}:jv"
        )
        tv = _mask_money_attr(
            "tasks_value", obj, getattr(obj, "tasks_value", 0) or 0, f"{seed}:tv"
        )
        rc = _as_count(getattr(obj, "retainer_count", None)) or 0
        if rc > 0:
            rv = round(float(rc) * 1_500.0 * 12 / max(rc, 1), 2)  # not used
            # Prefer demo annual from monthly if present, else count × £1.5k × 12 / clients
            rm = _as_float(getattr(obj, "retainer_monthly", 0) or 0)
            if rm > 0:
                # Scale original monthly into polished figure
                rv = demo_money(rm * 12, seed=f"{seed}:ret", context_key="retainer_annual")
            else:
                rv = round(float(rc) * 5_000.0, 2)
        else:
            rv = demo_money(
                getattr(obj, "retainer_annual", 0) or 0,
                seed=f"{seed}:ret",
                context_key="retainer_annual",
            )
        return float(jv) + float(tv) + float(rv)

    if kl in ("total_amount", "amount", "value", "total", "expected", "total_expected"):
        n = _as_count(count)
        if n is not None:
            unit = _unit_fee_for_band_label(band_key or jtype or kl)
            # If parent carries job_type (WipTypeHorizon), use that unit
            if jtype:
                unit = demo_fee_for_service(jtype)
            return round(float(n) * unit, 2) if n > 0 else 0.0

    return demo_money(
        val,
        seed,
        job_type=jtype,
        band_key=band_key,
        count=count,
        context_key=kl,
        is_task=is_task,
    )


def _job_type_from_row(row: dict) -> str:
    """Pull job/service type from a WIP/list dict row."""
    jtype = row.get("type") or row.get("job_type") or ""
    if jtype:
        return str(jtype)
    job = row.get("job")
    if job is None:
        return ""
    if isinstance(job, dict):
        return str(job.get("type") or job.get("job_type") or "")
    return str(getattr(job, "type", None) or getattr(job, "job_type", None) or "")


def anonymize_value(val: Any, depth: int = 0) -> Any:
    if depth > 10:
        return val
    if val is None or isinstance(val, (bool, str, bytes, date, datetime)):
        return val
    # Bare numbers without a key are left as-is (counts); money is keyed via parent
    if _is_numeric(val):
        return float(val) if isinstance(val, Decimal) else val
    if isinstance(val, DemoProxy):
        return val
    if isinstance(val, dict):
        out = {}
        # Pre-resolve type from nested job so amount uses the schedule even when fee is 0
        row_jtype = _job_type_from_row(val)
        row_band = val.get("key") or val.get("label") or val.get("name") or ""
        row_cnt = val.get("count")
        if row_cnt is None:
            row_cnt = val.get("total_count")
        for k, v in val.items():
            ks = str(k)
            kl = ks.lower()
            if kl in _SECRET_KEYS or "password" in kl:
                out[k] = (
                    _mask_secret(v)
                    if isinstance(v, (str, type(None)))
                    else anonymize_value(v, depth + 1)
                )
            elif kl == "label" and _is_structural_label(v):
                out[k] = v
            elif kl in _NAME_KEYS or kl.endswith("_name"):
                if isinstance(v, str) and v and v != "—":
                    if "person" in kl or "contact" in kl or kl == "full_name":
                        out[k] = demo_person_name(f"d:{ks}:{v}", v)
                    else:
                        out[k] = demo_company_name(f"d:{ks}:{v}", v)
                else:
                    out[k] = v
            elif kl in _CONTACT_KEYS:
                out[k] = demo_email(f"d:{ks}") if v else v
            elif kl in _ADDRESS_KEYS:
                out[k] = _mask_attr(ks, type("X", (), {"id": ks})(), v)
            elif _is_numeric(v) and _is_money_key(kl):
                jtype = row_jtype or val.get("type") or val.get("job_type") or ""
                is_task_row = (
                    "task" in kl
                    or "task" in str(row_band).lower()
                    or bool(val.get("is_from_email"))
                    or (
                        "title" in val
                        and "fee" in val
                        and "period_end" not in val
                        and "client_id" in val
                    )
                )
                # List rows: always use nested job type for fee/amount
                if kl in ("fee", "amount", "value", "gross_amount") and jtype and row_cnt is None:
                    out[k] = demo_fee_for_service(jtype)
                else:
                    out[k] = demo_money(
                        v,
                        f"d:{ks}:{v}",
                        job_type=jtype,
                        band_key=row_band,
                        count=row_cnt,
                        context_key=kl,
                        is_task=is_task_row and not jtype,
                    )
            else:
                out[k] = anonymize_value(v, depth + 1)
        return out
    if isinstance(val, (list, tuple)):
        seq = [anonymize_value(x, depth + 1) for x in val]
        return type(val)(seq) if not isinstance(val, list) else seq
    if isinstance(val, set):
        return {anonymize_value(x, depth + 1) for x in val}
    # SQLAlchemy / dataclass / simple objects (WipSnapshot, ageing rows, etc.)
    try:
        from dataclasses import is_dataclass

        if is_dataclass(val) and not isinstance(val, type):
            return DemoProxy(val)
    except Exception:
        pass
    if hasattr(val, "__mapper__") or hasattr(val, "__table__"):
        return DemoProxy(val)
    # Named tuples / simple objects / snapshots used in templates
    if not isinstance(val, type) and not callable(val):
        mod = getattr(type(val), "__module__", "") or ""
        if mod.startswith("app.") or hasattr(val, "id") or hasattr(val, "company_name"):
            return DemoProxy(val)
        d = getattr(val, "__dict__", {}) or {}
        money_attrs = (
            "value",
            "jobs_value",
            "tasks_value",
            "fee",
            "amount",
            "total",
            "balance",
            "retainer_annual",
            "retainer_monthly",
            "total_amount",
        )
        if any(hasattr(val, a) for a in money_attrs) or any(
            k in d
            for k in (
                "company_name",
                "full_name",
                "email",
                "fee",
                "amount",
                "value",
                "password",
            )
        ):
            return DemoProxy(val)
    return val


def anonymize_context(context: dict) -> dict:
    """Return a new context dict with confidential values anonymised."""
    skip = {
        "request",
        "demo_mode",
        "demo_locked",
        "today",
        "csrf_token",
        "hide_nav",
        "years",
        "statuses",
        "client_types",
        "job_types",
        "period",
        "year_label",
        "filter",
        "view",
        "msg",
        "error",
        "ch_key",
        "ch_msg",
        "ch_error",
        "reorder_enabled",
        "band_labels",
        "list_status_options",
        "mid_box_class",
        "mid_tile_class",
        "type_box_class",
        "type_tile_class",
        "show_age_home",
        "show_band_drill",
        "show_pe_year_home",
        "show_list",
        "filter_type",
        "filter_horizon",
        "filter_status",
        "filter_client_id",
        "filter_label",
        "filter_pe_key",
        "filter_pe_slice",
        "type_query",
    }
    out = {}
    for k, v in context.items():
        if k in skip:
            out[k] = v
            continue
        # Top-level money keys (total, wc_wip_value, tasks_value, …) incl. Decimal
        if _is_numeric(v) and _is_money_key(k):
            out[k] = _mask_top_level_money(k, v, context)
            continue
        out[k] = anonymize_value(v)
    out["demo_mode"] = True
    return out


def _mask_top_level_money(key: str, val: Any, context: dict) -> float:
    """Demo fees for template root money fields (dashboard, WIP, lists)."""
    kl = (key or "").lower()
    job_n = _as_count(context.get("count"))
    if job_n is None:
        job_n = _as_count(context.get("wc_wip_count"))
    if job_n is None:
        job_n = _as_count(context.get("filter_count"))

    task_n = _as_count(context.get("tasks_count"))
    if task_n is None:
        task_n = _as_count(context.get("wc_wip_tasks_count"))
    if task_n is None:
        task_n = 0

    ret_n = _as_count(context.get("retainer_count"))
    if ret_n is None:
        ret_n = _as_count(context.get("wc_retainer_count"))
    if ret_n is None:
        ret_n = 0

    # Task totals
    if "task" in kl and "count" not in kl:
        n = task_n if task_n is not None else 0
        return round(float(n) * 750.0, 2) if n > 0 else 0.0

    # Job book value (blended demo average)
    if "jobs_value" in kl or kl.endswith("jobs_value"):
        n = job_n or 0
        return round(float(n) * 5_000.0, 2) if n > 0 else 0.0

    # Retainers
    if "retainer" in kl:
        if ret_n and ret_n > 0:
            if "monthly" in kl:
                return round(float(ret_n) * 1_500.0, 2)
            # annual
            return round(float(ret_n) * 1_500.0 * 12, 2)
        v = _as_float(val)
        return _bucket_money(v, key) if v else 0.0

    # Filter list total for a typed band
    if kl in ("filter_fee",) or "filter_fee" in kl:
        n = _as_count(context.get("filter_count")) or 0
        jtype = context.get("filter_type") or ""
        if n <= 0:
            return 0.0
        if jtype:
            return round(n * demo_fee_for_service(jtype), 2)
        return round(n * 5_000.0, 2)

    # Grand WIP / net totals: jobs + tasks + retainers when we have counts
    if kl in ("total", "value", "wc_wip_value", "wc_net") or (
        "wip" in kl and ("value" in kl or "total" in kl)
    ):
        jv = round(float(job_n or 0) * 5_000.0, 2) if job_n else 0.0
        tv = round(float(task_n or 0) * 750.0, 2) if task_n else 0.0
        rv = round(float(ret_n) * 1_500.0 * 12, 2) if ret_n else 0.0
        # If no counts available, fall back to polished bucket of original
        if not job_n and not task_n and not ret_n:
            v = _as_float(val)
            return _bucket_money(v, key) if v else 0.0
        # Prefer sum; if only original total and job count, jobs blend is enough
        s = jv + tv + rv
        if s > 0:
            return s
        v = _as_float(val)
        return _bucket_money(v, key) if v else 0.0

    # Debtors / cash / creditors / pipeline — polished buckets (or count × unit)
    cnt = None
    if "debtor" in kl:
        cnt = context.get("wc_debtors_count") or context.get("count")
    elif "creditor" in kl:
        cnt = context.get("wc_creditors_count")
    elif "prospect" in kl or "pipeline" in kl:
        cnt = context.get("prospecting_open") or context.get("total_prospects")
    n = _as_count(cnt)
    if n is not None and n > 0 and _as_float(val) == 0:
        # Empty live figure but items exist — still show demo money
        return round(float(n) * 4_000.0, 2)
    if n is not None and n > 0:
        return demo_money(val, seed=key, count=n, context_key=key)
    v = _as_float(val)
    if v == 0:
        return 0.0
    return _bucket_money(v, key)


def should_block_export(path: str) -> bool:
    """Block data export / bulk download paths while demo mode is on."""
    p = (path or "").lower()
    if p.startswith("/export"):
        return True
    if "/export/" in p:
        return True
    if p.endswith("/export") or p.endswith(".csv") and "import" not in p:
        return True
    # Restore download / JSON dump
    if "backup" in p and "restore" not in p:
        return True
    return False
