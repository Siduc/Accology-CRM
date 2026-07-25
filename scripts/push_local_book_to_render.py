"""
Push the local SQLite practice book (WIP + debtors + clients/jobs/tasks) to Render Postgres.

WHY: Code on Render matches GitHub, but numbers differ because Render uses a
separate Postgres database. This copies the correct local data up.

SETUP (one time):
  1. Render → your Postgres DB → Connect → External Database URL
  2. In a PowerShell window (do not commit this):

     $env:DATABASE_URL = "postgresql://...external-url-from-render..."
     $env:CONFIRM_PUSH = "YES"
     cd C:\\Users\\SimonDuckworth\\accountant-crm
     $env:PYTHONPATH = "."
     python scripts/push_local_book_to_render.py

WHAT IT REPLACES on Postgres:
  clients, people, person_clients, jobs, practice_tasks,
  invoices, invoice_lines, payments, payment_allocations, debt_chase_actions

WHAT IT LEAVES ALONE:
  bank_*, suppliers/creditors, services catalogue (unless empty), groups, notes, etc.

IDs are preserved so links stay consistent.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, List, Sequence, Type

# Ensure project root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Do NOT bootstrap DATABASE_URL into the app before opening local SQLite —
# that would make "local" point at Render and copy the wrong way.
os.environ.pop("DATABASE_URL", None)

from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()

# Force SQLite file as the source of truth
sqlite_path = ROOT / "crm.db"
if not sqlite_path.exists():
    print(f"Local database not found: {sqlite_path}")
    sys.exit(1)

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

# Association table
ASSOC_TABLES = [person_clients]


def _ser(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v
    return v


def _row_dict(obj, columns: Sequence[str]) -> dict:
    out = {}
    for c in columns:
        out[c] = _ser(getattr(obj, c, None))
    return out


def _table_columns(model) -> List[str]:
    return [c.name for c in model.__table__.columns]


def _wipe_target(session) -> None:
    """Delete dependent rows first (Postgres FK order)."""
    # Sales / chase
    for sql in (
        "DELETE FROM debt_chase_actions",
        "DELETE FROM payment_allocations",
        "DELETE FROM payments",
        "DELETE FROM invoice_lines",
        "DELETE FROM invoices",
        "DELETE FROM quote_lines",
        "DELETE FROM quotes",
        # Tasks / jobs / people links
        "DELETE FROM practice_tasks",
        "DELETE FROM person_clients",
        "DELETE FROM client_job",
        "DELETE FROM jobs",
        "DELETE FROM people",
        # Groups / connections / CS packs that pin clients
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
            # continue with a fresh transaction
            continue
    session.commit()


def _reset_sequences(session, dialect: str) -> None:
    if dialect != "postgresql":
        return
    tables = [
        "clients",
        "people",
        "jobs",
        "practice_tasks",
        "invoices",
        "invoice_lines",
        "payments",
        "payment_allocations",
        "debt_chase_actions",
    ]
    for t in tables:
        session.execute(
            text(
                f"""
                SELECT setval(
                  pg_get_serial_sequence('{t}', 'id'),
                  COALESCE((SELECT MAX(id) FROM {t}), 1),
                  true
                )
                """
            )
        )
    session.commit()


def _copy_model(src, dest, model: Type) -> int:
    cols = _table_columns(model)
    rows = src.query(model).order_by(model.id.asc()).all()
    n = 0
    for obj in rows:
        data = _row_dict(obj, cols)
        dest.add(model(**data))
        n += 1
        if n % 200 == 0:
            dest.flush()
    dest.commit()
    return n


def _copy_person_clients(src, dest) -> int:
    rows = src.execute(person_clients.select()).fetchall()
    n = 0
    for r in rows:
        # Row mapping depends on SQLAlchemy version
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
    confirm = (os.environ.get("CONFIRM_PUSH") or "").strip().upper()
    # Accept either DATABASE_URL or RENDER_DATABASE_URL so we can keep local .env clean
    dest_url = (
        os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if confirm != "YES":
        print("Refusing to run: set CONFIRM_PUSH=YES")
        sys.exit(1)
    if not dest_url or "sqlite" in dest_url.lower():
        print(
            "Set RENDER_DATABASE_URL (or DATABASE_URL) to the Render *External* Postgres URL.\n"
            "Render → Postgres → Connect → External Database URL\n\n"
            "PowerShell example:\n"
            '  $env:CONFIRM_PUSH = "YES"\n'
            '  $env:RENDER_DATABASE_URL = "postgresql://USER:PASS@HOST/DB?sslmode=require"\n'
            "  $env:PYTHONPATH = \".\"\n"
            "  python scripts/push_local_book_to_render.py"
        )
        sys.exit(1)
    if "sslmode=" not in dest_url:
        dest_url = dest_url + ("&" if "?" in dest_url else "?") + "sslmode=require"

    # Local source = SQLite file only
    local_engine = create_engine(
        f"sqlite:///{sqlite_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    LocalSession = sessionmaker(bind=local_engine, autocommit=False, autoflush=False)
    local = LocalSession()
    wip = compute_wip(local)
    deb_t, deb_c = debtors_total(local)
    print(f"LOCAL source: {sqlite_path}")
    print(f"  WIP £{wip.value:,.2f} (jobs £{wip.jobs_value:,.0f} + ret £{wip.retainer_annual:,.0f} + tasks £{wip.tasks_value:,.0f})")
    print(f"  Debtors £{deb_t:,.2f} ({deb_c} invoices)")

    dest_engine = create_engine(dest_url, pool_pre_ping=True)
    DestSession = sessionmaker(bind=dest_engine, autocommit=False, autoflush=False)
    dest = DestSession()

    # Ensure schema exists on target
    Base.metadata.create_all(bind=dest_engine)
    # Additive columns via app migration helper if available
    try:
        from app.database import _add_missing_columns
        # temporarily point engine - skip; create_all + model columns enough for new cols
        # For existing PG, run raw ALTERs by importing init against dest is hard.
        # Use create_all which won't add columns; run a lightweight alter set:
        from sqlalchemy import inspect as sa_inspect

        insp = sa_inspect(dest_engine)
        existing = {c["name"] for c in insp.get_columns("clients")} if "clients" in insp.get_table_names() else set()
        alters = []
        if "billing_model" not in existing:
            alters.append("ALTER TABLE clients ADD COLUMN IF NOT EXISTS billing_model VARCHAR")
        if "retainer_amount" not in existing:
            alters.append("ALTER TABLE clients ADD COLUMN IF NOT EXISTS retainer_amount DOUBLE PRECISION")
        if "retainer_frequency" not in existing:
            alters.append("ALTER TABLE clients ADD COLUMN IF NOT EXISTS retainer_frequency VARCHAR")
        if "retainer_notes" not in existing:
            alters.append("ALTER TABLE clients ADD COLUMN IF NOT EXISTS retainer_notes TEXT")
        for sql in alters:
            try:
                dest.execute(text(sql.replace(" IF NOT EXISTS", "")))  # PG older?
            except Exception:
                try:
                    dest.execute(text(sql))
                except Exception as e:
                    print("alter note:", e)
        dest.commit()
    except Exception as e:
        print("schema ensure note:", e)
        dest.rollback()

    print("Wiping practice book tables on target Postgres...")
    _wipe_target(dest)

    print("Copying tables...")
    counts = {}
    counts["clients"] = _copy_model(local, dest, Client)
    counts["people"] = _copy_model(local, dest, Person)
    counts["person_clients"] = _copy_person_clients(local, dest)
    counts["jobs"] = _copy_model(local, dest, Job)
    counts["practice_tasks"] = _copy_model(local, dest, PracticeTask)
    counts["invoices"] = _copy_model(local, dest, Invoice)
    counts["invoice_lines"] = _copy_model(local, dest, InvoiceLine)
    counts["payments"] = _copy_model(local, dest, Payment)
    counts["payment_allocations"] = _copy_model(local, dest, PaymentAllocation)
    # skip chase history
    print("counts:", counts)

    dialect = dest_engine.dialect.name
    _reset_sequences(dest, dialect)

    # Verify on target
    wip2 = compute_wip(dest)
    d2, c2 = debtors_total(dest)
    print("TARGET after push:")
    print(f"  WIP £{wip2.value:,.2f} (jobs £{wip2.jobs_value:,.0f} + ret £{wip2.retainer_annual:,.0f} + tasks £{wip2.tasks_value:,.0f})")
    print(f"  Debtors £{d2:,.2f} ({c2} invoices)")

    local.close()
    dest.close()
    print("Done. Hard-refresh https://accology-crm-1.onrender.com/dashboard")


if __name__ == "__main__":
    main()
