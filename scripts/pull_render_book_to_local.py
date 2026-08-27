"""
Copy the live Render practice book → local crm.db (every table).

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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

sqlite_path = ROOT / "crm.db"


def _env_url() -> str:
    values = dotenv_values(ROOT / ".env", encoding="utf-8") or {}
    return (
        os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or values.get("RENDER_DATABASE_URL")
        or values.get("DATABASE_URL")
        or ""
    ).strip()


def main() -> None:
    confirm = (os.environ.get("CONFIRM_PULL") or "").strip().upper()
    src_raw = _env_url()
    if confirm != "YES":
        print("Refusing to run: set CONFIRM_PULL=YES")
        sys.exit(1)
    if not src_raw or "sqlite" in src_raw.lower():
        print("Set RENDER_DATABASE_URL (or DATABASE_URL) to the Render External Postgres URL.")
        sys.exit(1)

    # Point the app engine at the dest file so we do not open an Ohio pool.
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

    src_url = postgres_url(src_raw)
    dest_url = sqlite_url(sqlite_path)

    if sqlite_path.exists():
        bak = ROOT / f"crm.db-before-pull-{datetime.now():%Y%m%d-%H%M}.db"
        shutil.copy2(sqlite_path, bak)
        print(f"Backed up existing crm.db → {bak.name}", flush=True)
        sqlite_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(sqlite_path) + suffix)
        if side.exists():
            side.unlink()

    print(f"SOURCE Render: {mask_url(src_url)}", flush=True)
    print(f"DEST local:    {sqlite_path}", flush=True)
    print("Connecting (skipping pre-copy WIP — that query is why Ohio feels slow)…", flush=True)
    src = make_engine(src_url, sqlite=False)
    dest = make_engine(dest_url, sqlite=True)

    print("Copying all tables…")
    counts = copy_book(src, dest)
    copied = sum(counts.values())
    print(f"rows copied: {copied} across {len(counts)} tables")

    Dest = sessionmaker(bind=dest, autocommit=False, autoflush=False)
    dest_sess = Dest()
    try:
        wip2 = compute_wip(dest_sess)
        d2, c2 = debtors_total(dest_sess)
    finally:
        dest_sess.close()
    print("LOCAL after pull:")
    print(f"  WIP £{wip2.value:,.2f}  Debtors £{d2:,.2f} ({c2} invoices)")
    src.dispose()
    dest.dispose()
    print("Done. Comment out DATABASE_URL in .env and restart the CRM to use local.")


if __name__ == "__main__":
    main()
