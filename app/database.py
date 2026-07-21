"""SQLAlchemy engine — SQLite (local) or PostgreSQL (production)."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL, IS_SQLITE

_engine_kwargs: dict = {"pool_pre_ping": True}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables and apply lightweight additive migrations."""
    from app import models  # noqa: F401 — register models

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _migrate_person_clients()
    _seed_service_fees()


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
