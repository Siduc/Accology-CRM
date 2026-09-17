"""Copy every practice-book table between SQLite and Postgres.

Used by pull_render_book_to_local.py and push_local_book_to_render.py so the
X1 local book and the 17:00 Render publish stay complete (campaigns, mail,
playbooks, bank, tokens — not just clients/invoices).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("accountant_crm.book_sync")

# site_visits stay on the serving app (Render for accology.co). Do not wipe
# public hits when the 17:00 local book is pushed.
SKIP_TABLES = {"sqlite_sequence", "alembic_version", "site_visits"}
BATCH = 250


def mask_url(url: str) -> str:
    if "@" not in (url or ""):
        return "(url)"
    try:
        return "…" + url.split("@", 1)[1][:90]
    except Exception:
        return "(url)"


def make_engine(url: str, *, sqlite: bool | None = None) -> Engine:
    if sqlite is None:
        sqlite = url.startswith("sqlite")
    kwargs: dict = {"pool_pre_ping": True}
    if sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 3
        kwargs["max_overflow"] = 2
        kwargs["pool_timeout"] = 30
        kwargs["connect_args"] = {"connect_timeout": 20}
    engine = create_engine(url, **kwargs)
    if sqlite:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("PRAGMA busy_timeout=8000"))
    return engine


def ensure_schema(engine: Engine) -> None:
    from app.database import Base
    from app import models  # noqa: F401 — register tables

    Base.metadata.create_all(bind=engine)


def _is_sqlite(engine: Engine) -> bool:
    return engine.dialect.name == "sqlite"


def _sorted_names(engine: Engine) -> List[str]:
    meta = MetaData()
    meta.reflect(bind=engine)
    names = [t.name for t in meta.sorted_tables if t.name not in SKIP_TABLES]
    extra = [
        t
        for t in inspect(engine).get_table_names()
        if t not in SKIP_TABLES and t not in names
    ]
    return names + extra


def _table(engine: Engine, name: str):
    meta = MetaData()
    meta.reflect(bind=engine, only=[name])
    return meta.tables[name]


def _python_type(col) -> Optional[type]:
    try:
        return col.type.python_type
    except Exception:
        return None


def coerce(value: Any, col, *, dest_sqlite: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, UUID):
        value = str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)

    col_type = col.type
    type_name = type(col_type).__name__.upper()
    py = _python_type(col)

    if isinstance(col_type, Boolean) or py is bool or "BOOL" in type_name:
        if isinstance(value, bool):
            return int(value) if dest_sqlite else value
        if isinstance(value, (int, float)):
            flag = bool(value)
            return int(flag) if dest_sqlite else flag
        if isinstance(value, str):
            flag = value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
            return int(flag) if dest_sqlite else flag
        return value

    if py in (dict, list) or "JSON" in type_name:
        if dest_sqlite:
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    if isinstance(col_type, (DateTime, Date)) and isinstance(value, str):
        return value
    return value


def _row_for_dest(
    mapping: Dict[str, Any], dest_cols, *, dest_sqlite: bool
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for col in dest_cols:
        if col.name not in mapping:
            continue
        out[col.name] = coerce(mapping[col.name], col, dest_sqlite=dest_sqlite)
    return out


def wipe_tables(engine: Engine, names: Sequence[str]) -> None:
    sqlite = _is_sqlite(engine)
    with engine.begin() as conn:
        if sqlite:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        elif engine.dialect.name == "postgresql":
            try:
                conn.execute(text("SET session_replication_role = replica"))
            except SQLAlchemyError:
                pass
        for name in reversed(list(names)):
            try:
                conn.execute(text(f'DELETE FROM "{name}"'))
            except SQLAlchemyError as exc:
                logger.warning("wipe skip %s: %s", name, exc)
        if engine.dialect.name == "postgresql":
            try:
                conn.execute(text("SET session_replication_role = DEFAULT"))
            except SQLAlchemyError:
                pass


def reset_pg_sequences(engine: Engine, names: Iterable[str]) -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for name in names:
            cols = {c["name"] for c in inspect(engine).get_columns(name)}
            if "id" not in cols:
                continue
            try:
                conn.execute(
                    text(
                        f"""
                        SELECT setval(
                          pg_get_serial_sequence('"{name}"', 'id'),
                          COALESCE((SELECT MAX(id) FROM "{name}"), 1),
                          true
                        )
                        """
                    )
                )
            except SQLAlchemyError as exc:
                logger.warning("sequence skip %s: %s", name, exc)


def copy_book(src: Engine, dest: Engine) -> Dict[str, int]:
    """Wipe dest tables that exist on both sides, then copy all shared tables."""
    ensure_schema(dest)
    dest_sqlite = _is_sqlite(dest)
    src_names = set(_sorted_names(src))
    dest_names = _sorted_names(dest)
    shared = [n for n in dest_names if n in src_names]
    missing_on_src = [n for n in dest_names if n not in src_names]
    extra_on_src = sorted(src_names - set(dest_names))
    if missing_on_src:
        print("  dest-only (left empty):", ", ".join(missing_on_src), flush=True)
    if extra_on_src:
        print("  src-only (skipped):", ", ".join(extra_on_src), flush=True)

    print(f"  wiping {len(shared)} tables on dest…", flush=True)
    wipe_tables(dest, shared)

    counts: Dict[str, int] = {}
    dest_sqlite_flag = dest_sqlite
    for name in shared:
        src_table = _table(src, name)
        dest_table = _table(dest, name)
        dest_col_names = {c.name for c in dest_table.columns}
        src_col_names = {c.name for c in src_table.columns}
        overlap = src_col_names & dest_col_names
        if not overlap:
            counts[name] = 0
            continue
        n = 0
        batch: List[Dict[str, Any]] = []
        with src.connect() as sconn, dest.begin() as dconn:
            if dest_sqlite_flag:
                dconn.execute(text("PRAGMA foreign_keys=OFF"))
            result = sconn.execute(src_table.select())
            for raw in result.mappings():
                mapping = dict(raw)
                row = _row_for_dest(
                    {k: mapping[k] for k in overlap},
                    [c for c in dest_table.columns if c.name in overlap],
                    dest_sqlite=dest_sqlite_flag,
                )
                if not row:
                    continue
                batch.append(row)
                if len(batch) >= BATCH:
                    dconn.execute(dest_table.insert(), batch)
                    n += len(batch)
                    batch = []
            if batch:
                dconn.execute(dest_table.insert(), batch)
                n += len(batch)
        counts[name] = n
        print(f"    {name}: {n}", flush=True)
    reset_pg_sequences(dest, shared)
    return counts


def sqlite_url(path) -> str:
    from pathlib import Path

    return f"sqlite:///{Path(path).resolve().as_posix()}"


def postgres_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    if "sslmode=" not in url and url.startswith("postgresql"):
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url
