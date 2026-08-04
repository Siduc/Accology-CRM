"""Encrypt / decrypt practice secrets (CH company auth codes, etc.)."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger("accountant_crm.secrets_crypto")

_PREFIX = "enc:v1:"


def _fernet():
    """Build a Fernet key from SECRET_KEY / SESSION_SECRET / dedicated env."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "cryptography package required for encrypted secrets — "
            "pip install cryptography"
        ) from exc

    raw = (
        (os.environ.get("SECRETS_ENCRYPT_KEY") or "").strip()
        or (os.environ.get("SESSION_SECRET") or "").strip()
        or (os.environ.get("SECRET_KEY") or "").strip()
    )
    if not raw:
        # Stable-enough dev fallback — still better than plain text; set SESSION_SECRET in prod
        raw = "accologise-dev-secrets-key-change-me"
        logger.warning("SECRETS_ENCRYPT_KEY / SESSION_SECRET not set — using weak dev key")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(plain: Optional[str]) -> Optional[str]:
    """Encrypt a secret for DB storage. Empty → None. Already encrypted → unchanged."""
    if plain is None:
        return None
    s = str(plain).strip()
    if not s:
        return None
    if s.startswith(_PREFIX):
        return s
    token = _fernet().encrypt(s.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(stored: Optional[str]) -> Optional[str]:
    """Decrypt if encrypted; return plain legacy values as-is."""
    if stored is None:
        return None
    s = str(stored).strip()
    if not s:
        return None
    if not s.startswith(_PREFIX):
        return s  # legacy plain text
    token = s[len(_PREFIX) :]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt secret")
        return None


def mask_secret(stored: Optional[str], *, visible: int = 2) -> str:
    """UI mask: show last few chars only."""
    plain = decrypt_secret(stored)
    if not plain:
        return ""
    if len(plain) <= visible:
        return "•" * len(plain)
    return "•" * max(4, len(plain) - visible) + plain[-visible:]


def is_encrypted(stored: Optional[str]) -> bool:
    return bool(stored and str(stored).startswith(_PREFIX))


def secrets_ready() -> bool:
    try:
        _fernet()
        return True
    except Exception:
        return False
