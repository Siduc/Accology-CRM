from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    """Render a Jinja2 template with the current Starlette TemplateResponse API."""
    return templates.TemplateResponse(
        request, name, context or {}, status_code=status_code
    )
