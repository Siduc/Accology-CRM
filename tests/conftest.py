"""Isolate tests from the X1 live crm.db and production .env."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="accologise-pytest-"))
_TEST_DB = _TEST_DIR / "test.db"

# Set before any app import so dotenv cannot fill live secrets into blanks.
os.environ["ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["AUTH_USERNAME"] = "teststaff"
os.environ["AUTH_PASSWORD"] = "test-password-not-live"
os.environ["SESSION_SECRET"] = "t" * 32
os.environ["DEMO_AUTH_USERNAME"] = "demo"
os.environ["DEMO_AUTH_PASSWORD"] = "demo-password"
os.environ["TASK_IMPORT_API_KEY"] = "test-api-key-not-live"
os.environ["CHASE_LIVE_MODE"] = "0"
os.environ["CH_XML_SUBMIT_LIVE"] = "0"
os.environ["XAI_API_KEY"] = "test-not-live"
os.environ["XERO_CLIENT_ID"] = ""
os.environ["XERO_CLIENT_SECRET"] = "test-not-live"
os.environ["QBO_CLIENT_ID"] = ""
os.environ["QBO_CLIENT_SECRET"] = "test-not-live"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """App client against a throwaway SQLite file, never crm.db."""
    from app.database import init_db
    from app.main import app

    init_db()
    with TestClient(app) as c:
        yield c
