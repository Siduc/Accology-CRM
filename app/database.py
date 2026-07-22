"""SQLAlchemy engine — SQLite (local) or PostgreSQL (production / DATABASE_URL)."""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL, DB_DIALECT, DB_HOST, IS_SQLITE

logger = logging.getLogger("accountant_crm.database")

_engine_kwargs: dict = {"pool_pre_ping": True}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Render Postgres: modest pool, recycle idle connections, fail fast
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_recycle"] = 300
    _engine_kwargs["connect_args"] = {"connect_timeout": 10}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Safe startup hint for Render logs (never log the full URL / password)
if IS_SQLITE:
    logger.info("Database: sqlite (local file)")
else:
    logger.info("Database: %s host=%s", DB_DIALECT, DB_HOST or "(unknown)")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping_database() -> bool:
    """Return True if a simple query succeeds."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def init_db():
    """Create tables and apply lightweight additive migrations."""
    from app import models  # noqa: F401 — register models

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _migrate_person_clients()
    _seed_service_fees()
    _seed_sales_ledger()


def _seed_sales_ledger():
    """Seed Services catalogue; backfill invoices from jobs once."""
    from app.services.sales_ledger import backfill_invoices_from_jobs, seed_services

    db = SessionLocal()
    try:
        seed_services(db)
        # Only auto-backfill if sales ledger is empty
        from app.models.sales import Invoice

        if db.query(Invoice).count() == 0:
            backfill_invoices_from_jobs(db)
    except Exception:
        db.rollback()
    finally:
        db.close()


def _seed_service_fees():
    from app.services.fees import seed_default_fees

    db = SessionLocal()
    try:
        seed_default_fees(db)
    finally:
        db.close()


def _add_missing_columns():
    """Additive column migration for existing DBs (SQLite + Postgres)."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    alterations = {
        "clients": [
            ("created_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
            ("updated_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
            ("source", "VARCHAR"),
            ("accounts_software_id", "VARCHAR"),
            ("accounts_software_password", "VARCHAR"),
            ("ch_authentication_code", "VARCHAR"),
            ("ch_personal_code", "VARCHAR"),
            ("engagement_date", "DATE"),
            ("disengagement_date", "DATE"),
        ],
        "people": [
            ("client_id", "INTEGER"),
            ("is_primary", "INTEGER DEFAULT 0" if IS_SQLITE else "BOOLEAN DEFAULT FALSE"),
            ("is_individual_client", "INTEGER DEFAULT 0" if IS_SQLITE else "BOOLEAN DEFAULT FALSE"),
        ],
        "jobs": [
            ("created_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
            ("updated_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
            ("source", "VARCHAR"),
            ("invoice_reference", "VARCHAR"),
            ("billing_status", "VARCHAR"),
            ("gross_amount", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("vat_amount", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("was_late", "VARCHAR"),
            ("lost_reason", "VARCHAR"),
            ("import_key", "VARCHAR"),
            ("asana_task_gid", "VARCHAR"),
            ("asana_synced_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
        ],
        "debt_chase_actions": [
            ("stage", "VARCHAR"),
            ("channel", "VARCHAR"),
            ("email_to", "VARCHAR"),
            ("email_subject", "VARCHAR"),
            ("email_body", "TEXT" if not IS_SQLITE else "TEXT"),
            ("send_status", "VARCHAR"),
        ],
        "cs_packs": [
            ("ch_transaction_id", "VARCHAR"),
            ("filing_prep_json", "TEXT"),
            ("oauth_token_id", "INTEGER"),
        ],
        "bank_accounts": [
            ("bank_name", "VARCHAR"),
            ("sort_code", "VARCHAR"),
            ("account_number", "VARCHAR"),
            ("currency", "VARCHAR"),
            ("is_active", "INTEGER DEFAULT 1" if IS_SQLITE else "BOOLEAN DEFAULT TRUE"),
            ("is_primary", "INTEGER DEFAULT 0" if IS_SQLITE else "BOOLEAN DEFAULT FALSE"),
            ("notes", "TEXT" if not IS_SQLITE else "TEXT"),
        ],
        "bank_transactions": [
            ("reference", "VARCHAR"),
            ("counterparty", "VARCHAR"),
            ("category", "VARCHAR"),
            ("source", "VARCHAR"),
            ("reconciled", "INTEGER DEFAULT 0" if IS_SQLITE else "BOOLEAN DEFAULT FALSE"),
            ("reconciled_at", "TIMESTAMP" if not IS_SQLITE else "DATETIME"),
            ("import_hash", "VARCHAR"),
            ("matched_type", "VARCHAR"),
            ("matched_id", "INTEGER"),
            ("notes", "TEXT" if not IS_SQLITE else "TEXT"),
        ],
        "creditor_bills": [
            ("bank_transaction_id", "INTEGER"),
            ("supplier_id", "INTEGER"),
            ("number", "VARCHAR"),
            ("notes", "TEXT" if not IS_SQLITE else "TEXT"),
            ("issue_date", "DATE"),
            ("subtotal", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("vat_total", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("total", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("amount_paid", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
            ("balance", "FLOAT" if IS_SQLITE else "DOUBLE PRECISION"),
        ],
    }

    with engine.begin() as conn:
        for table, cols in alterations.items():
            if table not in existing_tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in cols:
                if col_name not in existing:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )


def _migrate_person_clients():
    """Copy legacy people.client_id links into person_clients (many-to-many)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "person_clients" not in tables or "people" not in tables:
        return

    people_cols = {c["name"] for c in inspector.get_columns("people")}
    if "client_id" not in people_cols:
        return

    if IS_SQLITE:
        sql = """
            INSERT OR IGNORE INTO person_clients (person_id, client_id, role, is_primary)
            SELECT p.id, p.client_id, p.role, COALESCE(p.is_primary, 0)
            FROM people p
            WHERE p.client_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM clients c WHERE c.id = p.client_id)
        """
    else:
        sql = """
            INSERT INTO person_clients (person_id, client_id, role, is_primary)
            SELECT p.id, p.client_id, p.role, COALESCE(p.is_primary, FALSE)
            FROM people p
            WHERE p.client_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM clients c WHERE c.id = p.client_id)
            ON CONFLICT DO NOTHING
        """

    with engine.begin() as conn:
        conn.execute(text(sql))
