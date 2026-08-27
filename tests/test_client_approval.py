from app.database import SessionLocal
from app.models.client import Client
from app.models.job import Job
from app.services.client_approval import mint_token, read_token

_n = 0


def _make_job():
    global _n
    _n += 1
    db = SessionLocal()
    try:
        client = Client(
            company_name=f"Approval Test {_n} Ltd",
            company_number=f"{_n:08d}",
        )
        db.add(client)
        db.flush()
        job = Job(
            title="Accounts YE 2026",
            type="Accounts",
            client_id=client.id,
            status="Review",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_token_roundtrip():
    assert read_token(mint_token(42)) == 42
    assert read_token("not-a-token") is None


def test_public_approval_without_login(client):
    job_id = _make_job()
    token = mint_token(job_id)
    r = client.get(f"/approve/{token}")
    assert r.status_code == 200
    assert b"Please review" in r.content
    assert b"Accounts YE 2026" in r.content


def test_client_can_approve(client):
    job_id = _make_job()
    token = mint_token(job_id)
    r = client.post(
        f"/approve/{token}",
        data={"decision": "approved", "actor": "Jane Client"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert job.client_approval_status == "approved"
        assert job.client_approval_by == "Jane Client"
        assert job.client_approval_at is not None
    finally:
        db.close()


def test_bad_token_is_not_a_login_redirect(client):
    r = client.get("/approve/totally-bogus", follow_redirects=False)
    assert r.status_code == 400
    loc = r.headers.get("location") or ""
    assert "/login" not in loc


def test_staff_link_requires_login(client):
    client.get("/logout")
    job_id = _make_job()
    r = client.get(f"/jobs/{job_id}/approval-link", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in (r.headers.get("location") or "")
