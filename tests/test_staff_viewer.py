from app.database import SessionLocal
from app.models import Client
from app.models.staff_user import StaffUser
from app.services.staff_auth import hash_password

VIEWER_USER = "melissa-test"
VIEWER_PASS = "melissa-viewer-test-pass"
# Unique 8-digit company numbers (throwaway sqlite only).
CN_VIEWER = "88001001"
CN_PRINCIPAL = "88001002"


def _ensure_viewer():
    db = SessionLocal()
    try:
        row = db.query(StaffUser).filter(StaffUser.username == VIEWER_USER).first()
        if row is None:
            db.add(
                StaffUser(
                    username=VIEWER_USER,
                    password_hash=hash_password(VIEWER_PASS),
                    display_name="Melissa Test",
                    role="viewer",
                    is_active=True,
                )
            )
        else:
            row.role = "viewer"
            row.is_active = True
            row.password_hash = hash_password(VIEWER_PASS)
        db.commit()
    finally:
        db.close()


def _company_exists(number: str) -> bool:
    db = SessionLocal()
    try:
        return (
            db.query(Client)
            .filter(Client.company_number.in_([number, number.zfill(8)]))
            .first()
            is not None
        )
    finally:
        db.close()


def test_viewer_login_succeeds(client):
    _ensure_viewer()
    client.get("/logout")
    r = client.post(
        "/login",
        data={"username": VIEWER_USER, "password": VIEWER_PASS},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in (r.headers.get("location") or "")
    client.get("/logout")


def test_viewer_get_clients_ok(client):
    _ensure_viewer()
    client.get("/logout")
    client.post(
        "/login",
        data={"username": VIEWER_USER, "password": VIEWER_PASS},
        follow_redirects=False,
    )
    r = client.get("/clients", follow_redirects=False)
    assert r.status_code == 200
    assert b"View only" in r.content
    client.get("/logout")


def test_viewer_post_client_forbidden(client):
    _ensure_viewer()
    client.get("/logout")
    client.post(
        "/login",
        data={"username": VIEWER_USER, "password": VIEWER_PASS},
        follow_redirects=False,
    )
    r = client.post(
        "/clients/new",
        data={
            "company_name": "Viewer Blocked Ltd",
            "company_number": CN_VIEWER,
            "overall_status": "Active",
        },
        follow_redirects=False,
    )
    assert r.status_code not in (200, 204)
    assert r.status_code in (403, 303)
    loc = (r.headers.get("location") or "").lower()
    if r.status_code in (303, 307):
        assert "/login" not in loc
        assert "view-only" in loc or "view only" in loc
    else:
        assert b"view only" in r.content.lower()
    assert not _company_exists(CN_VIEWER)
    client.get("/logout")


def test_principal_can_post_client(client):
    client.get("/logout")
    r = client.post(
        "/login",
        data={"username": "teststaff", "password": "test-password-not-live"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.post(
        "/clients/new",
        data={
            "company_name": "Principal Write Ltd",
            "company_number": CN_PRINCIPAL,
            "overall_status": "Active",
        },
        follow_redirects=False,
    )
    assert r.status_code != 403
    assert r.status_code in (200, 303)
    assert _company_exists(CN_PRINCIPAL)
    client.get("/logout")
