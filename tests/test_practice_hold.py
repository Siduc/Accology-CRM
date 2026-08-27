from datetime import date, timedelta

from app.database import SessionLocal
from app.models.client import Client
from app.models.cs_pack import CsPack
from app.models.job import Job
from app.services.practice_hold import is_held, set_hold, clear_hold

_n = 0


def _unique_cn() -> str:
    global _n
    _n += 1
    return f"77{_n:06d}"


def _make_client(**kwargs):
    db = SessionLocal()
    try:
        data = {
            "company_name": f"Hold Test {_n + 1} Ltd",
            "company_number": _unique_cn(),
            "overall_status": "Active",
            "client_type": "Limited Company",
        }
        data.update(kwargs)
        client = Client(**data)
        db.add(client)
        db.commit()
        db.refresh(client)
        return client.id
    finally:
        db.close()


def test_is_held_false_by_default():
    cid = _make_client()
    db = SessionLocal()
    try:
        client = db.get(Client, cid)
        assert client.on_hold in (0, None, False)
        assert is_held(client) is False
        assert client.is_on_hold() is False
        assert client.hold_label() == ""
    finally:
        db.close()


def test_set_hold_then_board_skips_open_shows_on_hold_filter():
    db = SessionLocal()
    try:
        client = Client(
            company_name=f"Hold Board {_n + 1} Ltd",
            company_number=_unique_cn(),
            overall_status="Active",
            client_type="Limited Company",
        )
        db.add(client)
        db.flush()
        due = date.today() + timedelta(days=10)
        job = Job(
            title="Confirmation Statement",
            type="Confirmation Statement",
            client_id=client.id,
            status="In Progress",
            statutory_due_date=due,
            period_end=due - timedelta(days=14),
        )
        db.add(job)
        db.flush()
        pack = CsPack(
            client_id=client.id,
            job_id=job.id,
            company_number=client.company_number,
            status="in_review",
            due_on=due,
            made_up_to=job.period_end,
        )
        db.add(pack)
        db.commit()
        db.refresh(client)
        cid = client.id
        assert is_held(client) is False
        set_hold(db, client, reason="bad_payer", note="pytest", by="teststaff")
        assert is_held(client) is True
        assert client.hold_reason == "bad_payer"
    finally:
        db.close()

    from app.services.cs_readiness import list_cs_readiness_board

    db = SessionLocal()
    try:
        open_board = list_cs_readiness_board(db, filter_key="open")
        open_ids = [r["client"].id for r in open_board["rows"]]
        assert cid not in open_ids
        assert open_board["counts"]["held"] >= 1
        held_board = list_cs_readiness_board(db, filter_key="on_hold")
        held_ids = [r["client"].id for r in held_board["rows"]]
        assert cid in held_ids
        row = next(r for r in held_board["rows"] if r["client"].id == cid)
        assert row.get("on_hold") is True
        clear_hold(db, db.get(Client, cid), by="teststaff")
        assert is_held(db.get(Client, cid)) is False
    finally:
        db.close()


def test_hold_post_requires_login_then_sets(client):
    cid = _make_client()
    client.get("/logout")
    r = client.post(
        f"/clients/{cid}/hold",
        data={"reason": "bad_payer", "note": "debt"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 307)
    loc = r.headers.get("location") or ""
    assert "/login" in loc

    login = client.post(
        "/login",
        data={"username": "teststaff", "password": "test-password-not-live"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    r = client.post(
        f"/clients/{cid}/hold",
        data={"reason": "bad_payer", "note": "debt"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        row = db.get(Client, cid)
        assert row.on_hold == 1
        assert row.hold_reason == "bad_payer"
        assert row.hold_set_by == "teststaff"
        assert is_held(row) is True
    finally:
        db.close()
