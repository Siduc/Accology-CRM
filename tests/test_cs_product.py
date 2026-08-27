"""£10 CS product tariff vs book (£50) schedule. Throwaway sqlite only."""

from datetime import date

from app.database import SessionLocal, init_db

init_db()
from app.models.client import Client
from app.models.job import Job
from app.models.sales import Service
from app.services.fees import CS_PRODUCT_FEE, get_suggested_fee, is_cs_product
from app.services.sales_ledger import CS_DISBURSEMENT_NET, invoice_from_job, is_disbursement_line

_n = 0


def _unique_cn() -> str:
    global _n
    _n += 1
    return f"91{_n:06d}"


def _make_client(db, **kwargs):
    data = {
        "company_name": f"CS Product Test {_n + 1} Ltd",
        "company_number": _unique_cn(),
        "overall_status": "Active",
        "client_type": "Limited Company",
    }
    data.update(kwargs)
    client = Client(**data)
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


def test_default_client_suggested_cs_fee_is_schedule_not_10():
    db = SessionLocal()
    try:
        client = _make_client(db)
        assert (client.cs_tariff or "book") == "book"
        assert client.is_cs_product() is False
        assert is_cs_product(client) is False
        fee = get_suggested_fee(
            db,
            "Confirmation Statement",
            period_end=date(2026, 8, 25),
            client_id=client.id,
        )
        assert fee == 50.0
        assert fee != CS_PRODUCT_FEE
    finally:
        db.close()


def test_product_tariff_client_suggested_fee_is_10():
    db = SessionLocal()
    try:
        client = _make_client(db, cs_tariff="product")
        assert client.is_cs_product() is True
        fee = get_suggested_fee(
            db,
            "Confirmation Statement",
            period_end=date(2026, 8, 25),
            client_id=client.id,
        )
        assert fee == 10.0
        # Even a prior £50 book job must not uplift a product client
        prior = Job(
            title="Confirmation Statement",
            type="Confirmation Statement",
            client_id=client.id,
            status="Completed",
            period_end=date(2025, 8, 25),
            fee=50.0,
        )
        db.add(prior)
        db.commit()
        fee2 = get_suggested_fee(
            db,
            "Confirmation Statement",
            period_end=date(2026, 8, 25),
            client_id=client.id,
        )
        assert fee2 == 10.0
    finally:
        db.close()


def test_book_client_prior_50_still_uplifts_not_10():
    db = SessionLocal()
    try:
        client = _make_client(db, cs_tariff="book")
        db.add(
            Job(
                title="Confirmation Statement",
                type="Confirmation Statement",
                client_id=client.id,
                status="Completed",
                period_end=date(2025, 6, 30),
                fee=50.0,
            )
        )
        db.commit()
        fee = get_suggested_fee(
            db,
            "Confirmation Statement",
            period_end=date(2026, 6, 30),
            client_id=client.id,
        )
        assert fee == 52.5
        assert fee != 10.0
    finally:
        db.close()


def test_invoice_from_job_product_cs_adds_ch_disb_50_zero_vat():
    db = SessionLocal()
    try:
        client = _make_client(db, cs_tariff="product")
        job = Job(
            title="Confirmation Statement",
            type="Confirmation Statement",
            client_id=client.id,
            status="In Progress",
            period_end=date(2026, 8, 25),
            fee=10.0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        inv = invoice_from_job(db, job, status="draft", source="pytest")
        lines = list(inv.lines or [])
        assert len(lines) == 2
        fee_lines = [ln for ln in lines if not is_disbursement_line(ln)]
        disb_lines = [ln for ln in lines if is_disbursement_line(ln)]
        assert len(fee_lines) == 1
        assert len(disb_lines) == 1
        assert float(fee_lines[0].unit_price) == 10.0
        assert float(fee_lines[0].vat_rate) == 0.2
        assert float(disb_lines[0].unit_price) == CS_DISBURSEMENT_NET == 50.0
        assert float(disb_lines[0].vat_rate) == 0.0
        svc = db.query(Service).filter(Service.code == "CS_PRODUCT").first()
        assert svc is not None
        assert float(svc.default_fee) == 10.0
        book_cs = db.query(Service).filter(Service.code == "CS").first()
        assert book_cs is not None
        assert float(book_cs.default_fee) == 50.0
    finally:
        db.close()


def test_cs_tariff_post_requires_login_then_sets(client):
    db = SessionLocal()
    try:
        row = _make_client(db)
        cid = row.id
    finally:
        db.close()

    client.get("/logout")
    r = client.post(
        f"/clients/{cid}/cs-tariff",
        data={"cs_tariff": "product"},
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
        f"/clients/{cid}/cs-tariff",
        data={"cs_tariff": "product"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        row = db.get(Client, cid)
        assert row.cs_tariff == "product"
        assert row.is_cs_product() is True
    finally:
        db.close()
