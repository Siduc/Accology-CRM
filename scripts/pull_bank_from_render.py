"""
Pull bank accounts (opening balances) from Render Postgres → local SQLite.

Local and Render are separate databases. Code deploys do not copy numbers.
Use this when you change bank opening balance (or account details) on Render
and want the same values on your PC.

SETUP:
  Render → Postgres → Connect → External Database URL

PowerShell:
  $env:RENDER_DATABASE_URL = "postgresql://USER:PASS@HOST/DB?sslmode=require"
  $env:CONFIRM_PULL = "YES"
  cd C:\\Users\\SimonDuckworth\\accountant-crm
  $env:PYTHONPATH = "."
  python scripts/pull_bank_from_render.py

WHAT IT UPDATES on local crm.db:
  bank_accounts — match by id when possible, else by name;
  creates missing accounts from Render.
  Copies: name, bank_name, sort_code, account_number, currency,
          opening_balance, is_active, is_primary, notes

WHAT IT LEAVES ALONE:
  bank_transactions, all clients/jobs/invoices, etc.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Never point the app at Render for this script's local session
os.environ.pop("DATABASE_URL", None)

from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()

sqlite_path = ROOT / "crm.db"
if not sqlite_path.exists():
    print(f"Local database not found: {sqlite_path}")
    sys.exit(1)


def _mask_url(url: str) -> str:
    if "@" not in url:
        return "(url)"
    try:
        return "…" + url.split("@", 1)[1][:80]
    except Exception:
        return "(url)"


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
        print(
            "Set RENDER_DATABASE_URL to the Render *External* Postgres URL.\n"
            "Render → Postgres → Connect → External Database URL\n\n"
            "PowerShell:\n"
            '  $env:CONFIRM_PULL = "YES"\n'
            '  $env:RENDER_DATABASE_URL = "postgresql://USER:PASS@HOST/DB?sslmode=require"\n'
            '  $env:PYTHONPATH = "."\n'
            "  python scripts/pull_bank_from_render.py"
        )
        sys.exit(1)
    if "sslmode=" not in src_url:
        src_url = src_url + ("&" if "?" in src_url else "?") + "sslmode=require"

    local_engine = create_engine(
        f"sqlite:///{sqlite_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    remote_engine = create_engine(src_url, pool_pre_ping=True)

    print(f"SOURCE Render: {_mask_url(src_url)}")
    print(f"DEST local:    {sqlite_path}")

    with remote_engine.connect() as rconn:
        rows = rconn.execute(
            text(
                """
                SELECT id, name, bank_name, sort_code, account_number, currency,
                       opening_balance, is_active, is_primary, notes
                FROM bank_accounts
                ORDER BY id
                """
            )
        ).mappings().all()

    if not rows:
        print("No bank_accounts on Render.")
        sys.exit(0)

    print("Render bank accounts:")
    for row in rows:
        print(
            f"  id={row['id']} {row['name']!r} "
            f"opening=£{float(row['opening_balance'] or 0):,.2f} "
            f"primary={bool(row['is_primary'])}"
        )

    updated = 0
    created = 0
    with local_engine.begin() as lconn:
        for row in rows:
            existing = lconn.execute(
                text("SELECT id FROM bank_accounts WHERE id = :id"),
                {"id": row["id"]},
            ).first()
            if not existing:
                by_name = lconn.execute(
                    text(
                        "SELECT id FROM bank_accounts WHERE lower(name) = lower(:n) LIMIT 1"
                    ),
                    {"n": row["name"] or "Practice account"},
                ).first()
                if by_name:
                    existing = by_name

            params = {
                "name": row["name"] or "Practice account",
                "bank_name": row["bank_name"],
                "sort_code": row["sort_code"],
                "account_number": row["account_number"],
                "currency": row["currency"] or "GBP",
                "opening_balance": float(row["opening_balance"] or 0),
                "is_active": 1 if row["is_active"] in (True, 1, "1", "t") else 0,
                "is_primary": 1 if row["is_primary"] in (True, 1, "1", "t") else 0,
                "notes": row["notes"],
            }

            if existing:
                params["id"] = existing[0]
                lconn.execute(
                    text(
                        """
                        UPDATE bank_accounts SET
                          name = :name,
                          bank_name = :bank_name,
                          sort_code = :sort_code,
                          account_number = :account_number,
                          currency = :currency,
                          opening_balance = :opening_balance,
                          is_active = :is_active,
                          is_primary = :is_primary,
                          notes = :notes
                        WHERE id = :id
                        """
                    ),
                    params,
                )
                updated += 1
                print(
                    f"  updated local id={params['id']} "
                    f"opening=£{params['opening_balance']:,.2f}"
                )
            else:
                params["id"] = row["id"]
                lconn.execute(
                    text(
                        """
                        INSERT INTO bank_accounts (
                          id, name, bank_name, sort_code, account_number, currency,
                          opening_balance, is_active, is_primary, notes
                        ) VALUES (
                          :id, :name, :bank_name, :sort_code, :account_number, :currency,
                          :opening_balance, :is_active, :is_primary, :notes
                        )
                        """
                    ),
                    params,
                )
                created += 1
                print(
                    f"  created local id={params['id']} "
                    f"opening=£{params['opening_balance']:,.2f}"
                )

    print(f"Done. updated={updated} created={created}")
    print("Refresh Cash / Bank on local (http://127.0.0.1:8000/bank).")


if __name__ == "__main__":
    main()
