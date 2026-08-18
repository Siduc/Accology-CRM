"""
Copy the live Render practice book → local crm.db.

Use this once before switching the X1 onto SQLite, so local is not the
July snapshot. Inverse of push_local_book_to_render.py.

  $env:CONFIRM_PULL = "YES"
  $env:PYTHONPATH = "."
  python scripts/pull_render_book_to_local.py

Reads RENDER_DATABASE_URL (or DATABASE_URL) from the environment / .env.
Backs up crm.db to crm.db-before-pull-YYYYMMDD-HHMM.db first.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Sequence, Type

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.pop("DATABASE_URL", None)

from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()

from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Client,
    Invoice,
    InvoiceLine,
    Job,
    Payment,
    PaymentAllocation,
    Person,
    PracticeTask,
    person_clients,
)
from app.models.sales import DebtChaseAction  # noqa: E402
from app.services.sales_ledger import debtors_total  # noqa: E402
from app.services.working_capital import compute_wip  # noqa: E402

sqlite_path = ROOT / "crm.db"

MODELS_IN_ORDER: List[Type] = [
    Client,
    Person,
    Job,
    PracticeTask,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    DebtChaseAction,
]


def _mask_url(url: str) -> str:
    if "@" not in url:
        return "(url)"
    try:
        return "…" + url.split("@", 1)[1][:80]
    except Exception:
        return "(url)"


def _ser(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v
    return v


def _table_columns(model) -> List[str]:
    return [c.name for c in model.__table__.columns]


def _row_dict(obj, columns: Sequence[str]) -> dict:
    return {c: _ser(getattr(obj, c, None)) for c in columns}


def _wipe_local(session) -> None:
    for sql in (
        "DELETE FROM debt_chase_actions",
        "DELETE FROM payment_allocations",
        "DELETE FROM payments",
        "DELETE FROM invoice_lines",
        "DELETE FROM invoices",
        "DELETE FROM quote_lines",
        "DELETE FROM quotes",
        "DELETE FROM practice_tasks",
        "DELETE FROM person_clients",
        "DELETE FROM client_job",
        "DELETE FROM jobs",
        "DELETE FROM people",
        "DELETE FROM practice_group_members",
        "DELETE FROM client_connections",
        "DELETE FROM cs_packs",
        "DELETE FROM clients",
    ):
        try:
            session.execute(text(sql))
        except Exception as e:
            session.rollback()
            print(f"  skip {sql}: {e}")
            continue
    session.commit()


def _copy_model(src, dest, model: Type) -> int:
    cols = _table_columns(model)
    rows = src.query(model).order_by(model.id.asc()).all()
    n = 0
    for obj in rows:
        dest.add(model(**_row_dict(obj, cols)))
        n += 1
        if n % 200 == 0:
            dest.flush()
    dest.commit()
    return n


def _copy_person_clients(src, dest) -> int:
    rows = src.execute(person_clients.select()).fetchall()
    n = 0
    for r in rows:
        try:
            d = dict(r._mapping)
        except Exception:
            d = {
                "person_id": r[0],
                "client_id": r[1],
                "role": r[2] if len(r) > 2 else None,
                "is_primary": r[3] if len(r) > 3 else False,
            }
        dest.execute(person_clients.insert().values(**d))
        n += 1
    dest.commit()
    return n


def main() -> None:
    confirm = (os.environ.get("CONFIRM_PULL") or "").strip().upper()
    src_url = (
        os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if confirm != "YES":
        print("Refusing to run: set CONFIRM_PULL=YES")
        sys.exit(1)
    if not src_url or "sqlite" in src_url.lower():
        print("Set RENDER_DATABASE_URL (or DATABASE_URL) to the Render External Postgres URL.")
        sys.exit(1)
    if "sslmode=" not in src_url:
        src_url = src_url + ("&" if "?" in src_url else "?") + "sslmode=require"

    if sqlite_path.exists():
        bak = ROOT / f"crm.db-before-pull-{datetime.now():%Y%m%d-%H%M}.db"
        shutil.copy2(sqlite_path, bak)
        print(f"Backed up existing crm.db → {bak.name}")

    src_engine = create_engine(src_url, pool_pre_ping=True)
    local_engine = create_engine(
        f"sqlite:///{sqlite_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=local_engine)
    Src = sessionmaker(bind=src_engine, autocommit=False, autoflush=False)
    Dest = sessionmaker(bind=local_engine, autocommit=False, autoflush=False)
    src = Src()
    dest = Dest()

    wip = compute_wip(src)
    deb_t, deb_c = debtors_total(src)
    print(f"SOURCE Render: {_mask_url(src_url)}")
    print(f"  WIP £{wip.value:,.2f}  Debtors £{deb_t:,.2f} ({deb_c} invoices)")
    print(f"DEST local:    {sqlite_path}")

    print("Wiping practice book tables on local crm.db...")
    _wipe_local(dest)

    print("Copying tables...")
    counts = {
        "clients": _copy_model(src, dest, Client),
        "people": _copy_model(src, dest, Person),
        "person_clients": _copy_person_clients(src, dest),
        "jobs": _copy_model(src, dest, Job),
        "practice_tasks": _copy_model(src, dest, PracticeTask),
        "invoices": _copy_model(src, dest, Invoice),
        "invoice_lines": _copy_model(src, dest, InvoiceLine),
        "payments": _copy_model(src, dest, Payment),
        "payment_allocations": _copy_model(src, dest, PaymentAllocation),
    }
    print("counts:", counts)

    wip2 = compute_wip(dest)
    d2, c2 = debtors_total(dest)
    print("LOCAL after pull:")
    print(f"  WIP £{wip2.value:,.2f}  Debtors £{d2:,.2f} ({c2} invoices)")

    src.close()
    dest.close()
    print("Done. Comment out DATABASE_URL in .env and restart the CRM to use local.")


if __name__ == "__main__":
    main()
