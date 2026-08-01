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


def _apply_dotenv_file(path: Path, *, log) -> bool:
    """
    Load a dotenv file without clobbering real host env values.

    - Never overrides a non-empty OS env var (Render / shell win).
    - Does fill keys that are missing or blank, so empty placeholders
      cannot block MS_GRAPH_* / secrets from project ``.env``.
    """
    if not path.is_file():
        log.info("dotenv path=%s exists=False", path)
        return False

    try:
        from dotenv import dotenv_values

        values = dotenv_values(path, encoding="utf-8") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("dotenv parse failed path=%s err=%s", path, exc)
        # Fallback: classic load (no override)
        ok = load_dotenv(path, override=False, encoding="utf-8")
        log.info("dotenv path=%s exists=True loaded=%s (fallback)", path, bool(ok))
        return bool(ok)

    filled = 0
    for key, val in values.items():
        if key is None or val is None:
            continue
        current = os.environ.get(key)
        if current is not None and str(current).strip() != "":
            continue  # host / already-set wins
        os.environ[key] = str(val)
        filled += 1

    log.info(
        "dotenv path=%s exists=True keys=%s filled_blank_or_missing=%s",
        path,
        len(values),
        filled,
    )
    return True


def bootstrap_environment(*, force: bool = False) -> bool:
    """
    Load dotenv files once (or again when force=True).

    Returns True if at least one .env file was applied.
    Safe to call repeatedly; use force=True after editing .env while the
    process is running (Settings / OAuth will re-check MS Graph).
    """
    global _BOOTSTRAPPED, DOTENV_LOADED, DOTENV_FILES_LOADED
    if _BOOTSTRAPPED and not force:
        return DOTENV_LOADED

    _ensure_logging()
    log = logging.getLogger("accountant_crm.env_bootstrap")

    loaded_any = False
    DOTENV_FILES_LOADED = []

    # Base .env first, then optional production overlay
    for path in (DOTENV_PATH, DOTENV_PRODUCTION_PATH):
        if _apply_dotenv_file(path, log=log):
            loaded_any = True
            DOTENV_FILES_LOADED.append(str(path))

    # Also honour process cwd .env only if different (rare)
    cwd_env = Path.cwd() / ".env"
    try:
        same = cwd_env.is_file() and cwd_env.resolve() == DOTENV_PATH.resolve()
    except OSError:
        same = False
    if cwd_env.is_file() and not same:
        if _apply_dotenv_file(cwd_env, log=log):
            loaded_any = True
            DOTENV_FILES_LOADED.append(str(cwd_env))

    DOTENV_LOADED = loaded_any
    _BOOTSTRAPPED = True

    env_name = (os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or "").strip()
    has_db = bool((os.environ.get("DATABASE_URL") or "").strip())
    ms_id = bool((os.environ.get("MS_GRAPH_CLIENT_ID") or "").strip())
    ms_secret = bool((os.environ.get("MS_GRAPH_CLIENT_SECRET") or "").strip())
    log.info(
        "bootstrap complete ENV=%r DATABASE_URL_set=%s "
        "MS_GRAPH_CLIENT_ID_set=%s MS_GRAPH_CLIENT_SECRET_set=%s dotenv_files=%s",
        env_name or "(unset)",
        has_db,
        ms_id,
        ms_secret,
        DOTENV_FILES_LOADED or "[]",
    )
    return DOTENV_LOADED


# Run immediately on import — earliest possible for `import app.env_bootstrap`
bootstrap_environment()
