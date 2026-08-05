from pathlib import Path
from datetime import date

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def _fmt_uk_date(value) -> str:
    if value is None or value == "":
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d-%m-%Y")
    return str(value)


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


templates.env.filters["uk_date"] = _fmt_uk_date
templates.env.filters["job_overdue"] = _job_is_overdue
templates.env.filters["job_status"] = _job_display_status
templates.env.filters["norm_caps"] = _normalize_caps
templates.env.filters["norm_person"] = _normalize_person_name
templates.env.filters["urlquote"] = _urlquote


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
    templates.env.filters.setdefault("norm_caps", _normalize_caps)
    templates.env.filters.setdefault("norm_person", _normalize_person_name)
    templates.env.filters.setdefault("urlquote", _urlquote)

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
