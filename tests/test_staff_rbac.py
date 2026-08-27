from app.database import SessionLocal
from app.models.staff_user import StaffUser
from app.services.staff_auth import hash_password
from app.services.staff_rbac import is_principal_only_path


def test_principal_only_paths():
    assert is_principal_only_path("/settings/xero")
    assert is_principal_only_path("/oauth/xero/start")
    assert not is_principal_only_path("/clients/1/xero/pull")
    assert not is_principal_only_path("/dashboard")


def test_staff_blocked_from_practice_xero(client):
    db = SessionLocal()
    try:
        if not db.query(StaffUser).filter(StaffUser.username == "melissa").first():
            db.add(
                StaffUser(
                    username="melissa",
                    password_hash=hash_password("melissa-test-pass"),
                    role="staff",
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()

    client.get("/logout")
    r = client.post(
        "/login",
        data={"username": "melissa", "password": "melissa-test-pass"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    denied = client.get("/settings/xero", follow_redirects=False)
    assert denied.status_code == 403
    allowed = client.get("/dashboard", follow_redirects=False)
    assert allowed.status_code == 200


def test_principal_can_open_xero_settings(client):
    client.get("/logout")
    r = client.post(
        "/login",
        data={"username": "teststaff", "password": "test-password-not-live"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/settings/xero", follow_redirects=False)
    assert page.status_code == 200


from types import SimpleNamespace

from app.services.staff_rbac import (
    is_principal,
    is_read_only,
    is_viewer,
    is_write_method,
    viewer_blocked_write,
)


def _req(role: str, method: str = "GET", path: str = "/clients"):
    return SimpleNamespace(
        session={"staff_role": role, "user": "u"},
        method=method,
        url=SimpleNamespace(path=path),
    )


def test_viewer_role_helpers():
    v = _req("viewer")
    assert is_viewer(v)
    assert is_read_only(v)
    assert not is_principal(v)
    assert is_write_method("POST")
    assert is_write_method("put")
    assert not is_write_method("GET")
    assert viewer_blocked_write(_req("viewer", "POST", "/clients/new"))
    assert not viewer_blocked_write(_req("viewer", "GET", "/clients"))
    assert not viewer_blocked_write(_req("viewer", "POST", "/logout"))
    assert not viewer_blocked_write(_req("viewer", "POST", "/login"))
    assert not viewer_blocked_write(_req("principal", "POST", "/clients/new"))
    assert not viewer_blocked_write(_req("staff", "POST", "/clients/new"))
