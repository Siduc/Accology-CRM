"""
Earliest environment bootstrap for Accountant CRM.

Must be imported before reading ENV / DATABASE_URL anywhere.
- Loads project-root `.env` (and optional `.env.production`) via python-dotenv
- Does NOT override variables already set by the host (Render / shell)
- Configures logging so INFO lines appear on Render
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"
DOTENV_PRODUCTION_PATH = PROJECT_ROOT / ".env.production"

_BOOTSTRAPPED = False
DOTENV_FILES_LOADED: List[str] = []
DOTENV_LOADED = False


def _ensure_logging() -> None:
    """Make sure INFO logs are visible (Render captures stdout/stderr)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s [%(name)s] %(message)s",
        )
    # Our loggers should not be quieter than INFO in production debugging
    logging.getLogger("accountant_crm").setLevel(logging.INFO)


def bootstrap_environment() -> bool:
    """
    Load dotenv files once. Returns True if at least one .env file was loaded.

    Safe to call repeatedly.
    """
    global _BOOTSTRAPPED, DOTENV_LOADED, DOTENV_FILES_LOADED
    if _BOOTSTRAPPED:
        return DOTENV_LOADED

    _ensure_logging()
    log = logging.getLogger("accountant_crm.env_bootstrap")

    loaded_any = False
    DOTENV_FILES_LOADED = []

    # Base .env first, then optional production overlay (still override=False)
    for path in (DOTENV_PATH, DOTENV_PRODUCTION_PATH):
        exists = path.is_file()
        if exists:
            ok = load_dotenv(path, override=False, encoding="utf-8")
            if ok:
                loaded_any = True
                DOTENV_FILES_LOADED.append(str(path))
            log.info(
                "dotenv path=%s exists=True loaded=%s override=False",
                path,
                bool(ok),
            )
        else:
            log.info("dotenv path=%s exists=False", path)

    # Also honour process cwd .env only if different (rare) — still no override
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file() and cwd_env.resolve() != DOTENV_PATH.resolve():
        ok = load_dotenv(cwd_env, override=False, encoding="utf-8")
        log.info("dotenv cwd path=%s loaded=%s", cwd_env, bool(ok))
        if ok:
            loaded_any = True
            DOTENV_FILES_LOADED.append(str(cwd_env))

    DOTENV_LOADED = loaded_any
    _BOOTSTRAPPED = True

    env_name = (os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or "").strip()
    has_db = bool((os.environ.get("DATABASE_URL") or "").strip())
    log.info(
        "bootstrap complete ENV=%r DATABASE_URL_set=%s dotenv_files=%s",
        env_name or "(unset)",
        has_db,
        DOTENV_FILES_LOADED or "[]",
    )
    return DOTENV_LOADED


# Run immediately on import — earliest possible for `import app.env_bootstrap`
bootstrap_environment()
