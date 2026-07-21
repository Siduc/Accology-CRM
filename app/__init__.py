"""Accountant CRM application package.

Load project-root `.env` as early as possible (before any submodule config reads).
OS / host environment variables always take precedence (override=False).
"""

from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)
