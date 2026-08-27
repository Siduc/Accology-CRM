"""Staff role checks. Missing session role = principal (existing Simon sessions)."""

from __future__ import annotations

from starlette.requests import Request

PRINCIPAL_ONLY_PREFIXES = (
    "/settings/xero",
    "/oauth/xero/start",
    "/oauth/xero/disconnect",
    "/settings/sage",
    "/oauth/sage/start",
    "/oauth/sage/disconnect",
    "/settings/qbo",
    "/oauth/qbo/start",
    "/oauth/qbo/disconnect",
)

# POST login must still work (session role is set after authenticate).
# Logout must still work after login.
VIEWER_WRITE_EXEMPT = frozenset({"/logout", "/login"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def staff_role(request: Request) -> str:
    return (request.session.get("staff_role") or "principal").strip() or "principal"


def is_principal(request: Request) -> bool:
    return staff_role(request) == "principal"


def is_viewer(request: Request) -> bool:
    return staff_role(request) == "viewer"


def is_read_only(request: Request) -> bool:
    return is_viewer(request)


def is_write_method(method: str | None) -> bool:
    return (method or "").strip().upper() in _WRITE_METHODS


def _path_only(path: str) -> str:
    return (path or "").split("?", 1)[0]


def is_principal_only_path(path: str) -> bool:
    p = _path_only(path)
    return any(p == prefix or p.startswith(prefix + "/") for prefix in PRINCIPAL_ONLY_PREFIXES)


def viewer_blocked_write(request: Request) -> bool:
    """True if viewer and a write method, except logout (and login)."""
    if not is_viewer(request):
        return False
    if not is_write_method(getattr(request, "method", None)):
        return False
    path = _path_only(getattr(getattr(request, "url", None), "path", "") or "")
    if path in VIEWER_WRITE_EXEMPT:
        return False
    return True
