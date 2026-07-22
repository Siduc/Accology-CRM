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


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    """Render a Jinja2 template with the current Starlette TemplateResponse API."""
    ctx = dict(context or {})
    ctx.setdefault("today", date.today())
    return templates.TemplateResponse(
        request, name, ctx, status_code=status_code
    )
