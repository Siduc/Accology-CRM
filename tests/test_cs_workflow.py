from datetime import date, timedelta
from types import SimpleNamespace

from app.database import SessionLocal, init_db

init_db()
from app.models.client import Client
from app.models.cs_pack import CsPack
from app.models.job import Job
from app.models.sales import Invoice
from app.services.cs_automation import mark_filed
from app.services.cs_readiness import (
    stamp_code_requested,
    workflow_stage,
)

_n = 0


def _unique_cn() -> str:
    global _n
    _n += 1
    return f"8{_n:07d}"


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def test_workflow_stage_awaiting_auth_code():
    client = _ns(ch_authentication_code=None)
    pack = _ns(
        status="draft",
        due_on=date.today() + timedelta(days=10),
        code_requested_at=None,
        code_received_at=None,
        code_request_note=None,
    )
    job = _ns(
        status="Planned",
        statutory_due_date=pack.due_on,
        billing_status=None,
        invoice_reference=None,
    )
    assert workflow_stage(pack, job, client, today=date.today()) == "awaiting_auth_code"


def test_workflow_stage_late_when_due_in_the_past():
    client = _ns(ch_authentication_code="plain-legacy-code")
    yesterday = date.today() - timedelta(days=1)
    pack = _ns(
        status="in_review",
        due_on=yesterday,
        code_requested_at=None,
        code_received_at=None,
        code_request_note=None,
    )
    job = _ns(
        status="In Progress",
        statutory_due_date=yesterday,
        billing_status=None,
        invoice_reference=None,
    )
    assert workflow_stage(pack, job, client, today=date.today()) == "late"


def test_workflow_stage_filed_when_pack_filed():
    client = _ns(ch_authentication_code=None)
    pack = _ns(
        status="filed",
        due_on=date.today() - timedelta(days=5),
        code_requested_at=None,
        code_received_at=None,
        code_request_note=None,
    )
    job = _ns(
        status="Completed",
        statutory_due_date=pack.due_on,
        billing_status=None,
        invoice_reference=None,
    )
    assert workflow_stage(pack, job, client, today=date.today()) == "filed"


def _seed_cs(due=None, *, fee=50.0, status="In Progress"):
    db = SessionLocal()
    try:
        client = Client(
            company_name=f"CS Workflow {_n + 1} Ltd",
            company_number=_unique_cn(),
            overall_status="Active",
            client_type="Limited Company",
        )
        db.add(client)
        db.flush()
        job = Job(
            title="Confirmation Statement",
            type="Confirmation Statement",
            client_id=client.id,
            status=status,
            fee=fee,
            statutory_due_date=due,
            period_end=(due - timedelta(days=14)) if due else None,
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
        # Live numbering ignores INV-0001 placeholders; seed the floor once
        # so a second mark_filed in this suite can allocate INV-0101+.
        if not db.query(Invoice).filter(Invoice.number == "INV-0100").first():
            db.add(
                Invoice(
                    number="INV-0100",
                    client_id=client.id,
                    issue_date=date.today(),
                    due_date=date.today(),
                    status="void",
                    issuer="accology",
                    import_key="pytest-cs-inv-floor",
                    source="test",
                )
            )
        db.commit()
        db.refresh(pack)
        db.refresh(job)
        return pack.id, job.id, client.id
    finally:
        db.close()


def test_mark_filed_sets_was_late_when_due_yesterday():
    yesterday = date.today() - timedelta(days=1)
    pack_id, job_id, _ = _seed_cs(due=yesterday)
    db = SessionLocal()
    try:
        result = mark_filed(db, pack_id)
        assert result.ok
        job = db.get(Job, job_id)
        assert job.was_late == "yes"
        assert job.status == "Completed"
        pack = db.get(CsPack, pack_id)
        assert pack.status == "filed"
    finally:
        db.close()


def test_mark_filed_creates_draft_invoice_if_none():
    yesterday = date.today() - timedelta(days=1)
    pack_id, job_id, _ = _seed_cs(due=yesterday, fee=50.0)
    db = SessionLocal()
    try:
        before = db.query(Invoice).filter(Invoice.job_id == job_id).count()
        assert before == 0
        result = mark_filed(db, pack_id)
        assert result.ok
        inv = db.query(Invoice).filter(Invoice.job_id == job_id).first()
        assert inv is not None
        assert (inv.status or "").lower() == "draft"
        job = db.get(Job, job_id)
        assert job.invoice_reference
    finally:
        db.close()


def test_stamp_code_requested_sets_timestamp():
    pack_id, _, _ = _seed_cs(due=date.today() + timedelta(days=20))
    db = SessionLocal()
    try:
        result = stamp_code_requested(db, pack_id, note="chased director")
        assert result["ok"]
        pack = db.get(CsPack, pack_id)
        assert pack.code_requested_at is not None
        assert pack.code_request_note == "chased director"
        assert pack.code_received_at is None
        first_at = pack.code_requested_at
        stamp_code_requested(db, pack_id, note="second chase")
        pack = db.get(CsPack, pack_id)
        assert pack.code_requested_at == first_at
        assert pack.code_request_note == "second chase"
    finally:
        db.close()


def test_code_requested_post_requires_login(client):
    client.get("/logout")
    pack_id, _, _ = _seed_cs(due=date.today() + timedelta(days=7))
    r = client.post(
        f"/cs/{pack_id}/code-requested",
        data={"note": "chase"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 307)
    assert "/login" in (r.headers.get("location") or "")


def test_code_requested_post_after_login_redirects_to_pack(client):
    pack_id, _, _ = _seed_cs(due=date.today() + timedelta(days=7))
    login = client.post(
        "/login",
        data={"username": "teststaff", "password": "test-password-not-live"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    r = client.post(
        f"/cs/{pack_id}/code-requested",
        data={"note": "personal PIN chased"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers.get("location") or ""
    assert f"/cs/{pack_id}" in loc
    db = SessionLocal()
    try:
        pack = db.get(CsPack, pack_id)
        assert pack.code_requested_at is not None
        assert pack.code_request_note == "personal PIN chased"
    finally:
        db.close()
