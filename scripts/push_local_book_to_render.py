"""
Push the local SQLite practice book to Render Postgres (every table).

  $env:CONFIRM_PUSH = "YES"
  $env:PYTHONPATH = "."
  python scripts/push_local_book_to_render.py

Reads RENDER_DATABASE_URL (or DATABASE_URL). IDs are preserved.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

sqlite_path = ROOT / "crm.db"


def _dest_url() -> str:
    values = dotenv_values(ROOT / ".env", encoding="utf-8") or {}
    return (
        os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or values.get("RENDER_DATABASE_URL")
        or values.get("DATABASE_URL")
        or ""
    ).strip()


def main() -> None:
    confirm = (os.environ.get("CONFIRM_PUSH") or "").strip().upper()
    dest_raw = _dest_url()
    if confirm != "YES":
        print("Refusing to run: set CONFIRM_PUSH=YES")
        sys.exit(1)
    if not sqlite_path.exists():
        print(f"Local database not found: {sqlite_path}")
        sys.exit(1)
    if not dest_raw or "sqlite" in dest_raw.lower():
        print(
            "Set RENDER_DATABASE_URL (or DATABASE_URL) to the Render *External* Postgres URL."
        )
        sys.exit(1)

    # App engine must stay on the local file, not Ohio.
    os.environ["DATABASE_URL"] = f"sqlite:///{sqlite_path.as_posix()}"

    from scripts.book_sync import (  # noqa: E402
        copy_book,
        make_engine,
        mask_url,
        postgres_url,
        sqlite_url,
    )
    from app.services.sales_ledger import debtors_total  # noqa: E402
    from app.services.working_capital import compute_wip  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    src_url = sqlite_url(sqlite_path)
    dest_url = postgres_url(dest_raw)

    src = make_engine(src_url, sqlite=True)
    dest = make_engine(dest_url, sqlite=False)

    Local = sessionmaker(bind=src, autocommit=False, autoflush=False)
    local = Local()
    try:
        wip = compute_wip(local)
        deb_t, deb_c = debtors_total(local)
    finally:
        local.close()
    print(f"LOCAL source: {sqlite_path}")
    print(
        f"  WIP £{wip.value:,.2f} (jobs £{wip.jobs_value:,.0f} + ret £{wip.retainer_annual:,.0f} + tasks £{wip.tasks_value:,.0f})"
    )
    print(f"  Debtors £{deb_t:,.2f} ({deb_c} invoices)")
    print(f"DEST Render: {mask_url(dest_url)}")

    print("Copying all tables…")
    counts = copy_book(src, dest)
    print(f"rows copied: {sum(counts.values())} across {len(counts)} tables")

    Dest = sessionmaker(bind=dest, autocommit=False, autoflush=False)
    dest_sess = Dest()
    try:
        wip2 = compute_wip(dest_sess)
        d2, c2 = debtors_total(dest_sess)
    finally:
        dest_sess.close()
    print("TARGET after push:")
    print(
        f"  WIP £{wip2.value:,.2f} (jobs £{wip2.jobs_value:,.0f} + ret £{wip2.retainer_annual:,.0f} + tasks £{wip2.tasks_value:,.0f})"
    )
    print(f"  Debtors £{d2:,.2f} ({c2} invoices)")
    src.dispose()
    dest.dispose()
    print("Done. Hard-refresh https://accology-crm-1.onrender.com/dashboard")


if __name__ == "__main__":
    main()
