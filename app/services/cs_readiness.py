"""Practice CS readiness board: auth code, shares, pack, due dates, invoice."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import Client, Job
from app.models.cs_pack import CsPack
from app.services.company_numbers import normalize_company_number
from app.services.cs_automation import latest_pack_for_client
from app.services.share_register import (
    client_is_ch_entity,
    has_ch_auth_code,
    is_shareholder_row,
    list_holdings,
)


@dataclass
class ReadinessItem:
    key: str
    ok: bool
    label: str
    detail: str = ""


def assess_client(
    db: Session,
    client: Client,
    *,
    pack: Optional[CsPack] = None,
    job: Optional[Job] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Checklist for filing CS on WebFiling (practice prep, not CH API submit)."""
    today = today or date.today()
    items: List[Dict[str, Any]] = []

    def add(key: str, ok: bool, label: str, detail: str = "") -> None:
        items.append({"key": key, "ok": ok, "label": label, "detail": detail})

    cn = normalize_company_number(client.company_number or "")
    add(
        "company_number",
        bool(cn)
        and not cn.upper().startswith("IND-")
        and not cn.upper().startswith("PENDING"),
        "Valid company number",
        cn or "missing",
    )

    auth_ok = has_ch_auth_code(client)
    add(
        "auth_code",
        auth_ok,
        "CH company auth code",
        "Encrypted on file" if auth_ok else "Add on Shares / CH",
    )

    holdings = list_holdings(db, client.id)
    n_sh = sum(1 for h in holdings if is_shareholder_row(h))
    verified = bool(client.share_register_verified_at)
    shares_ok = verified and n_sh > 0
    if shares_ok:
        sh_detail = f"Verified · {n_sh} shareholder(s)"
    elif n_sh > 0:
        sh_detail = f"{n_sh} with counts — mark verified"
    elif holdings:
        sh_detail = f"{len(holdings)} potential — set share numbers"
    elif client.ch_register_seeded_at:
        sh_detail = "Seeded but empty — check CH pull"
    else:
        sh_detail = "Refresh from Companies House, then allocate shares"
    add("share_register", shares_ok, "Share register ready", sh_detail)

    if pack is None:
        try:
            pack = latest_pack_for_client(db, client.id)
        except Exception:
            pack = None
    pack_ok = bool(
        pack
        and (pack.status or "")
        in ("in_review", "ready_to_file", "filed")
    )
    add(
        "cs_pack",
        pack_ok,
        "CS review pack",
        f"{pack.status}" if pack else "Refresh from CH to create pack",
    )

    due = None
    if job and job.statutory_due_date:
        due = job.statutory_due_date
    elif pack and pack.due_on:
        due = pack.due_on
    due_ok = due is not None
    due_detail = due.isoformat() if due else "No CS due date on job/pack"
    if due:
        days = (due - today).days
        if days < 0:
            due_detail = f"{due.isoformat()} · { -days}d overdue"
        elif days <= 30:
            due_detail = f"{due.isoformat()} · due in {days}d"
        else:
            due_detail = f"{due.isoformat()} · in {days}d"
    add("due_date", due_ok, "CS due date known", due_detail)

    blocking = [i for i in items if not i["ok"]]
    # Due date missing is softer if pack has made_up_to — still block for board clarity
    n_block = len(blocking)
    if n_block == 0:
        level = "ready"
    elif n_block <= 2:
        level = "almost"
    else:
        level = "missing"

    days_to_due = None
    if due:
        days_to_due = (due - today).days

    return {
        "client": client,
        "pack": pack,
        "job": job,
        "checklist": items,
        "blocking": [i["key"] for i in blocking],
        "blocking_labels": [i["label"] for i in blocking],
        "ok_count": sum(1 for i in items if i["ok"]),
        "total": len(items),
        "level": level,
        "practice_ready": n_block == 0,
        "due": due,
        "days_to_due": days_to_due,
        "overdue": bool(due and days_to_due is not None and days_to_due < 0),
        "due_soon": bool(
            due and days_to_due is not None and 0 <= days_to_due <= 30
        ),
    }


def list_cs_readiness_board(
    db: Session,
    *,
    filter_key: str = "open",
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Board of CS work: open Confirmation Statement jobs + readiness.

    filter_key: open | overdue | due_soon | ready | almost | missing | all
    """
    today = today or date.today()
    jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.type == "Confirmation Statement")
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
        .order_by(Job.statutory_due_date.asc().nullslast(), Job.id.desc())
        .limit(400)
        .all()
    )

    rows: List[Dict[str, Any]] = []
    for job in jobs:
        client = job.client
        if not client:
            continue
        if (client.overall_status or "") == "Inactive":
            continue
        # Sole traders / partnerships are not CH CS01 work
        if not client_is_ch_entity(client):
            continue
        pack = latest_pack_for_client(db, client.id)
        row = assess_client(db, client, pack=pack, job=job, today=today)
        rows.append(row)

    # Also surface clients with open packs but no open CS job
    pack_client_ids = {r["client"].id for r in rows}
    packs = (
        db.query(CsPack)
        .filter(CsPack.status.in_(["draft", "in_review", "ready_to_file"]))
        .order_by(CsPack.due_on.asc().nullslast())
        .limit(200)
        .all()
    )
    for pack in packs:
        if pack.client_id in pack_client_ids:
            continue
        client = db.query(Client).filter(Client.id == pack.client_id).first()
        if not client or (client.overall_status or "") == "Inactive":
            continue
        job = None
        if pack.job_id:
            job = db.query(Job).filter(Job.id == pack.job_id).first()
        row = assess_client(db, client, pack=pack, job=job, today=today)
        rows.append(row)
        pack_client_ids.add(client.id)

    fk = (filter_key or "open").strip().lower()
    if fk == "overdue":
        rows = [r for r in rows if r.get("overdue")]
    elif fk in ("due_soon", "soon"):
        rows = [r for r in rows if r.get("due_soon") or r.get("overdue")]
    elif fk == "ready":
        rows = [r for r in rows if r.get("level") == "ready"]
    elif fk == "almost":
        rows = [r for r in rows if r.get("level") == "almost"]
    elif fk == "missing":
        rows = [r for r in rows if r.get("level") == "missing"]
    # open / all → all rows

    # Sort: overdue first, then due soon, then by due date, then missing worst first
    def sort_key(r: Dict[str, Any]):
        d = r.get("days_to_due")
        if d is None:
            d = 9999
        level_rank = {"missing": 0, "almost": 1, "ready": 2}.get(r.get("level"), 1)
        return (0 if r.get("overdue") else 1, d, level_rank)

    rows.sort(key=sort_key)

    return {
        "rows": rows,
        "filter": fk,
        "today": today,
        "counts": {
            "showing": len(rows),
            "ready": sum(1 for r in rows if r.get("level") == "ready"),
            "almost": sum(1 for r in rows if r.get("level") == "almost"),
            "missing": sum(1 for r in rows if r.get("level") == "missing"),
            "overdue": sum(1 for r in rows if r.get("overdue")),
            "due_soon": sum(1 for r in rows if r.get("due_soon")),
        },
    }


def mark_ready_with_invoice(
    db: Session,
    pack_id: int,
    *,
    raise_invoice: bool = False,
    fee: Optional[float] = None,
) -> Dict[str, Any]:
    """Mark CS pack ready; optionally raise sales invoice from linked job."""
    from app.services.cs_automation import get_pack, mark_ready
    from app.services.fees import get_suggested_fee
    from app.services.sales_ledger import find_invoice_for_job, invoice_from_job

    result = mark_ready(db, pack_id)
    if not result.ok or not result.pack:
        return {"ok": False, "error": result.error or "Could not mark ready", "pack": None}

    pack = result.pack
    inv = None
    inv_msg = ""
    if raise_invoice and pack.job_id:
        job = db.query(Job).filter(Job.id == pack.job_id).first()
        if job:
            existing = find_invoice_for_job(db, job)
            if existing:
                inv = existing
                inv_msg = f"Invoice {existing.number or existing.id} already exists."
            else:
                if fee is not None:
                    try:
                        job.fee = float(fee)
                    except (TypeError, ValueError):
                        pass
                if not job.fee or float(job.fee or 0) <= 0:
                    pe = job.period_end or pack.made_up_to or date.today()
                    try:
                        job.fee = float(
                            get_suggested_fee(
                                db,
                                "Confirmation Statement",
                                period_end=pe,
                                client_id=job.client_id,
                            )
                            or 50
                        )
                    except Exception:
                        job.fee = 50.0
                    db.commit()
                try:
                    inv = invoice_from_job(db, job, status="sent", source="cs_ready")
                    inv_msg = f"Invoice raised ({inv.number or inv.id})."
                except Exception as exc:
                    inv_msg = f"Invoice not raised: {exc}"
        else:
            inv_msg = "No CS job linked — invoice skipped."
    elif raise_invoice:
        inv_msg = "No job on pack — invoice skipped."

    return {
        "ok": True,
        "pack": pack,
        "invoice": inv,
        "message": inv_msg,
        "error": "",
    }
