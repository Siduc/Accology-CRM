"""
Demo / presentation mode — anonymise confidential data in the UI only.

Session flag `demo_mode` (bool). Real database is never changed.
Stable pseudonyms so the same client/person looks consistent while navigating.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Optional

SESSION_KEY = "demo_mode"

# Identity / contact fields
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
        "label",
        "practice_name",
        "ch_name",
    }
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

_COMPANY_A = (
    "North",
    "South",
    "Oak",
    "River",
    "Hill",
    "Castle",
    "Market",
    "Green",
    "Bridge",
    "Park",
    "Crown",
    "Sterling",
    "Atlas",
    "Beacon",
    "Cedar",
)
_COMPANY_B = (
    "Holdings",
    "Trading",
    "Services",
    "Solutions",
    "Group",
    "Partners",
    "Consulting",
    "Properties",
    "Logistics",
    "Media",
    "Design",
    "Engineering",
)
_FIRST = (
    "Alex",
    "Jordan",
    "Sam",
    "Taylor",
    "Morgan",
    "Casey",
    "Riley",
    "Jamie",
    "Avery",
    "Quinn",
    "Drew",
    "Blake",
    "Cameron",
    "Harper",
    "Reese",
)
_LAST = (
    "Taylor",
    "Morgan",
    "Brooks",
    "Hayes",
    "Reed",
    "Foster",
    "Bennett",
    "Coleman",
    "Hughes",
    "Patel",
    "Singh",
    "Walsh",
    "Murray",
    "Clarke",
    "Walsh",
)


def is_demo_request(request) -> bool:
    try:
        return bool(request.session.get(SESSION_KEY))
    except Exception:
        return False


def set_demo_mode(request, enabled: bool) -> None:
    request.session[SESSION_KEY] = bool(enabled)


def _h(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:12], 16)


def demo_company_name(key: Any, original: Any = None) -> str:
    n = _h(f"co:{key}:{original or ''}")
    return f"{_COMPANY_A[n % len(_COMPANY_A)]} {_COMPANY_B[(n // 7) % len(_COMPANY_B)]} Ltd"


def demo_person_name(key: Any, original: Any = None) -> str:
    n = _h(f"pe:{key}:{original or ''}")
    return f"{_FIRST[n % len(_FIRST)]} {_LAST[(n // 11) % len(_LAST)]}"


def demo_email(key: Any) -> str:
    n = _h(f"em:{key}") % 9000 + 1000
    return f"contact{n}@example.com"


def demo_phone(key: Any) -> str:
    n = _h(f"ph:{key}") % 10_000_000
    return f"07{n:09d}"[:11]


def demo_company_number(key: Any) -> str:
    n = _h(f"cn:{key}") % 90_000_000 + 10_000_000
    return f"{n:08d}"


def demo_money(val: Any, seed: Any = "") -> float:
    """Scramble amounts; keep zero and rough order of magnitude for charts."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return 0.0
    if v == 0 or math.isnan(v) or math.isinf(v):
        return 0.0
    sign = 1.0 if v > 0 else -1.0
    av = abs(v)
    mag = 10 ** max(0, int(math.log10(av))) if av >= 1 else 0.01
    h = _h(f"£:{seed}:{round(av, 2)}") % 900
    # 0.5x–1.4x of magnitude bucket, not the real figure
    factor = 0.5 + (h / 900.0) * 0.9
    out = sign * round(factor * mag * (1 + (h % 20) / 10.0), 2)
    if abs(out) < 0.01:
        out = sign * 0.01
    return out


def _is_money_key(key: str) -> bool:
    k = (key or "").lower()
    if k in ("id", "count", "year", "period", "status", "key", "pct", "n", "length"):
        return False
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

    if kl in _NAME_KEYS or kl.endswith("_name") or kl in ("title", "label"):
        if not val or val == "—":
            return val
        # Job titles / status labels that aren't people
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
        if kl in ("name", "label", "display_name", "practice_name", "ch_name"):
            # Heuristic: multi-word Title Case → person, else company-ish
            if "," in s or s.isupper():
                return demo_person_name(seed, s)
            return demo_company_name(seed, s)
        return demo_company_name(seed, s)

    if kl in ("notes", "body", "detail", "description", "message", "subject") and isinstance(
        val, str
    ):
        if not val.strip():
            return val
        return "[Anonymised in demo mode]"

    if isinstance(val, (int, float)) and not isinstance(val, bool) and _is_money_key(kl):
        return demo_money(val, seed)

    return anonymize_value(val)


def anonymize_value(val: Any, depth: int = 0) -> Any:
    if depth > 10:
        return val
    if val is None or isinstance(val, (bool, int, float, str, bytes, date, datetime)):
        return val
    if isinstance(val, DemoProxy):
        return val
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            ks = str(k)
            kl = ks.lower()
            if kl in _SECRET_KEYS or "password" in kl:
                out[k] = _mask_secret(v) if isinstance(v, (str, type(None))) else anonymize_value(v, depth + 1)
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
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and _is_money_key(kl):
                out[k] = demo_money(v, f"d:{ks}:{v}")
            else:
                out[k] = anonymize_value(v, depth + 1)
        return out
    if isinstance(val, (list, tuple)):
        seq = [anonymize_value(x, depth + 1) for x in val]
        return type(val)(seq) if not isinstance(val, list) else seq
    if isinstance(val, set):
        return {anonymize_value(x, depth + 1) for x in val}
    # SQLAlchemy / dataclass / simple objects
    if hasattr(val, "__mapper__") or hasattr(val, "__table__"):
        return DemoProxy(val)
    # Named tuples / simple objects with __dict__ used in templates
    if hasattr(val, "__dict__") and not isinstance(val, type):
        # Avoid wrapping modules, functions
        mod = getattr(type(val), "__module__", "") or ""
        if mod.startswith("app.") or hasattr(val, "id") or hasattr(val, "company_name"):
            return DemoProxy(val)
        # Generic: wrap if it has sensitive-looking attrs
        d = getattr(val, "__dict__", {})
        if any(
            k in d
            for k in (
                "company_name",
                "full_name",
                "email",
                "fee",
                "amount",
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
    }
    out = {}
    for k, v in context.items():
        if k in skip:
            out[k] = v
            continue
        out[k] = anonymize_value(v)
    out["demo_mode"] = True
    return out


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
