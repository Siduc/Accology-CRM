from pathlib import Path
from datetime import date

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def _fmt_uk_date(value) -> str:
    """Display dates as UK short form DD/MM/YYYY (never ISO YYYY-MM-DD)."""
    if value is None or value == "":
        return "—"
    if hasattr(value, "strftime"):
        # date / datetime
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)
    s = str(value).strip()
    if not s:
        return "—"
    # ISO date or datetime strings → UK short
    try:
        from datetime import date as _date
        from datetime import datetime as _dt

        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            # 2026-08-06 or 2026-08-06T12:00:00
            head = s[:10]
            d = _date.fromisoformat(head)
            return d.strftime("%d/%m/%Y")
        if len(s) >= 10 and s[2] == "-" and s[5] == "-":
            # already DD-MM-YYYY
            d = _dt.strptime(s[:10], "%d-%m-%Y").date()
            return d.strftime("%d/%m/%Y")
        if len(s) >= 10 and s[2] == "/" and s[5] == "/":
            # already DD/MM/YYYY (or D/M/Y variants longer)
            return s[:10] if len(s) >= 10 and s[6:10].isdigit() else s
    except Exception:
        pass
    return s


from urllib.parse import quote as _url_quote

from app.text_format import normalize_caps as _normalize_caps
from app.text_format import normalize_person_name as _normalize_person_name


def _urlquote(value) -> str:
    """Percent-encode a path/query for use as a return_to query value."""
    if value is None:
        return ""
    # Encode ? & so they don't break the outer query string
    return _url_quote(str(value), safe="")


def _job_is_overdue(job, today=None) -> bool:
    if job is None:
        return False
    if hasattr(job, "is_overdue"):
        return bool(job.is_overdue(today))
    return False


def _job_display_status(job, today=None) -> str:
    if job is None:
        return "—"
    if hasattr(job, "display_status"):
        return job.display_status(today)
    return getattr(job, "status", None) or "—"


def _job_label(job, with_client: bool = False) -> str:
    """Safe job title for templates (never raises)."""
    if job is None:
        return "—"
    try:
        if hasattr(job, "label") and callable(job.label):
            return job.label(with_client=bool(with_client)) or "—"
    except Exception:
        pass
    title = (getattr(job, "title", None) or "").strip()
    jtype = (getattr(job, "type", None) or "").strip()
    pe = getattr(job, "period_end", None)
    pe_s = _fmt_uk_date(pe) if pe is not None else ""
    if pe_s == "—":
        pe_s = ""
    if title:
        core = title
    elif jtype and pe_s:
        core = f"{jtype} — {pe_s}"
    elif jtype:
        core = jtype
    else:
        core = f"Job {getattr(job, 'id', '')}"
    if with_client:
        client = getattr(job, "client", None)
        if client is not None:
            try:
                cname = client.display_name() if hasattr(client, "display_name") else ""
            except Exception:
                cname = getattr(client, "company_name", None) or ""
            if cname:
                return f"{cname} · {core}"
    return core


def _service_schedule_label(svc) -> str:
    """Recurrence + quarterly pattern for Services catalogue (never raises)."""
    if svc is None:
        return "—"
    try:
        fn = getattr(type(svc), "schedule_label", None)
        if callable(fn):
            return fn(svc) or "—"
    except Exception:
        pass
    try:
        from app.models.sales import SERVICE_QUARTERLY_PATTERNS

        rec = (getattr(svc, "recurrence", None) or "none").strip().lower()
        if rec in ("", "none", "one_off", "one-off"):
            return "—"
        base = {
            "monthly": "Monthly",
            "quarterly": "Quarterly",
            "annually": "Annually",
            "annual": "Annually",
        }.get(rec, rec.replace("_", " ").title() or "—")
        if rec in ("quarterly", "annually", "annual"):
            code = (getattr(svc, "quarterly_pattern", None) or "").strip().lower()
            for key, label, _m in SERVICE_QUARTERLY_PATTERNS:
                if key == code:
                    return f"{base} · {label}"
        return base
    except Exception:
        return "—"


def _client_vat_scheme_label(client) -> str:
    """Client VAT frequency + stagger for detail screens (never raises)."""
    if client is None:
        return "—"
    try:
        fn = getattr(type(client), "vat_scheme_label", None)
        if callable(fn):
            return fn(client) or "—"
    except Exception:
        pass
    try:
        freq = (getattr(client, "vat_frequency", None) or "none").strip().lower()
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

            code = (getattr(client, "vat_quarterly_pattern", None) or "").strip().lower()
            for key, label, _m in SERVICE_QUARTERLY_PATTERNS:
                if key == code:
                    return f"{base} · {label}"
            ye = getattr(client, "vat_year_end_month", None)
            if ye:
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
                m = int(ye)
                if 1 <= m <= 12:
                    return f"{base} · YE {months[m]}"
        return base
    except Exception:
        return "—"


def _fmt_num(value, decimals: int = 0) -> str:
    """Number with UK thousands separators (1,234 or 1,234.56)."""
    if value is None or value == "":
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    d = int(decimals) if decimals is not None else 0
    if d <= 0:
        return f"{v:,.0f}"
    return f"{v:,.{d}f}"


def _fmt_money(value, decimals: int = 0) -> str:
    """Sterling with thousands separators: £1,234 or £1,234.56."""
    if value is None or value == "":
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    d = int(decimals) if decimals is not None else 0
    if d <= 0:
        return f"£{v:,.0f}"
    return f"£{v:,.{d}f}"


def _fmt_money2(value) -> str:
    """Sterling with pence and thousands separators."""
    return _fmt_money(value, 2)


templates.env.filters["uk_date"] = _fmt_uk_date
templates.env.filters["job_overdue"] = _job_is_overdue
templates.env.filters["job_status"] = _job_display_status
templates.env.filters["job_label"] = _job_label
templates.env.filters["service_schedule"] = _service_schedule_label
templates.env.filters["vat_scheme"] = _client_vat_scheme_label
templates.env.filters["norm_caps"] = _normalize_caps
templates.env.filters["norm_person"] = _normalize_person_name
templates.env.filters["urlquote"] = _urlquote
templates.env.filters["num"] = _fmt_num
templates.env.filters["money"] = _fmt_money
templates.env.filters["money2"] = _fmt_money2


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    """Render a Jinja2 template with the current Starlette TemplateResponse API."""
    from app.services.demo_mode import (
        anonymize_context,
        is_demo_locked,
        is_demo_request,
    )

    # Ensure filters are always registered (safe if module was partially reloaded)
    templates.env.filters.setdefault("uk_date", _fmt_uk_date)
    templates.env.filters.setdefault("job_overdue", _job_is_overdue)
    templates.env.filters.setdefault("job_status", _job_display_status)
    templates.env.filters.setdefault("job_label", _job_label)
    templates.env.filters.setdefault("service_schedule", _service_schedule_label)
    templates.env.filters.setdefault("num", _fmt_num)
    templates.env.filters.setdefault("money", _fmt_money)
    templates.env.filters.setdefault("money2", _fmt_money2)
    templates.env.filters["num"] = _fmt_num
    templates.env.filters["money"] = _fmt_money
    templates.env.filters["money2"] = _fmt_money2
    templates.env.filters.setdefault("vat_scheme", _client_vat_scheme_label)
    templates.env.filters.setdefault("norm_caps", _normalize_caps)
    templates.env.filters.setdefault("norm_person", _normalize_person_name)
    templates.env.filters.setdefault("urlquote", _urlquote)
    # Always refresh these (methods/filters evolve with catalogue / VAT work)
    templates.env.filters["service_schedule"] = _service_schedule_label
    templates.env.filters["vat_scheme"] = _client_vat_scheme_label

    ctx = dict(context or {})
    ctx.setdefault("today", date.today())
    demo = is_demo_request(request)
    locked = is_demo_locked(request)
    ctx["demo_mode"] = demo
    ctx["demo_locked"] = locked
    # Accology brand assets (logo) on every page
    try:
        from app.services.branding import practice_branding_context

        brand = practice_branding_context()
        for k, v in brand.items():
            ctx.setdefault(k, v)
        # Light logo for brand-blue / dark chrome
        from app.services.branding import BRANDING_DIR, static_url
        from pathlib import Path

        dark = BRANDING_DIR / "logo_on_dark.png"
        if dark.is_file():
            ctx.setdefault("practice_logo_on_dark_url", static_url(dark))
        else:
            ctx.setdefault(
                "practice_logo_on_dark_url",
                ctx.get("practice_logo_url") or "/static/branding/logo.png",
            )
    except Exception:
        ctx.setdefault("practice_logo_url", "/static/branding/logo.png")
        ctx.setdefault("practice_logo_on_dark_url", "/static/branding/logo_on_dark.png")
    # Staff notification badge (authenticated pages only)
    ctx.setdefault("notify_unread_count", 0)
    ctx.setdefault("notify_preview", [])
    if request.session.get("user") and not locked:
        try:
            from app.database import SessionLocal
            from app.services import notifications as notify_svc

            db = SessionLocal()
            try:
                # Raise job/task alert_on notifications into the top banner
                try:
                    notify_svc.sync_work_alerts(db)
                except Exception:
                    pass
                ctx["notify_unread_count"] = notify_svc.unread_count(db)
                ctx["notify_preview"] = notify_svc.list_unread(db, limit=5)
            finally:
                db.close()
        except Exception:
            pass
    if demo:
        # Anonymise confidential fields for presentation; DB is unchanged
        ctx = anonymize_context(ctx)
        ctx["demo_locked"] = locked
    return templates.TemplateResponse(
        request, name, ctx, status_code=status_code
    )
