from app.database import SessionLocal
from app.models.staff_user import StaffUser
from app.services.staff_auth import hash_password, verify_password


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)


def test_env_staff_is_seeded(client):
    db = SessionLocal()
    try:
        row = db.query(StaffUser).filter(StaffUser.username == "teststaff").first()
        assert row is not None
        assert row.role == "principal"
        assert row.password_hash.startswith("pbkdf2_sha256$")
    finally:
        db.close()


def test_second_staff_user_can_login(client):
    db = SessionLocal()
    try:
        db.add(
            StaffUser(
                username="staff2",
                password_hash=hash_password("melissa-test-pass"),
                display_name="Staff Two",
                role="staff",
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.post(
        "/login",
        data={"username": "staff2", "password": "melissa-test-pass"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in (r.headers.get("location") or "")


def test_inactive_staff_cannot_login(client):
    db = SessionLocal()
    try:
        db.add(
            StaffUser(
                username="leavers",
                password_hash=hash_password("nope"),
                role="staff",
                is_active=False,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.post(
        "/login",
        data={"username": "leavers", "password": "nope"},
        follow_redirects=False,
    )
    loc = r.headers.get("location") or ""
    assert "/dashboard" not in loc
