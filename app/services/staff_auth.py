"""Staff passwords (PBKDF2) and login against staff_users.

Does not touch client data. Seed copies AUTH_USERNAME/PASSWORD once if the
table is empty, so the existing shared login keeps working.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.staff_user import StaffUser

_ROUNDS = 200_000


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt.encode("utf-8"),
        _ROUNDS,
    )
    return f"pbkdf2_sha256${_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt, digest = (stored or "").split("$", 3)
        rounds = int(rounds_s)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256" or rounds < 1 or not salt or not digest:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt.encode("utf-8"),
        rounds,
    )
    return hmac.compare_digest(dk.hex(), digest)


def seed_staff_from_env(db: Session) -> Optional[StaffUser]:
    """If no staff rows exist, copy the env shared login as principal."""
    if db.query(StaffUser).count() > 0:
        return None
    from app import config

    username = (getattr(config, "AUTH_USERNAME", None) or "").strip()
    password = (getattr(config, "AUTH_PASSWORD", None) or "").strip()
    if not username or not password:
        return None
    user = StaffUser(
        username=username,
        password_hash=hash_password(password),
        display_name=username,
        role="principal",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_staff(db: Session, username: str, password: str) -> Optional[StaffUser]:
    """Log in any active staff_users row (principal, staff, or viewer)."""
    u = (username or "").strip()
    if not u or not password:
        return None
    seed_staff_from_env(db)
    # Role is not a login filter — viewer may authenticate when is_active.
    row = (
        db.query(StaffUser)
        .filter(StaffUser.username == u, StaffUser.is_active.is_(True))
        .first()
    )
    if row and verify_password(password, row.password_hash):
        row.last_login_at = datetime.utcnow()
        db.commit()
        return row

    # Fallback: env shared login still works even if the row was renamed.
    from app import config

    expected_u = (getattr(config, "AUTH_USERNAME", None) or "").strip()
    expected_p = (getattr(config, "AUTH_PASSWORD", None) or "").strip()
    if expected_u and expected_p and u == expected_u and len(password) == len(expected_p):
        if hmac.compare_digest(password, expected_p):
            if row is None:
                row = StaffUser(
                    username=expected_u,
                    password_hash=hash_password(expected_p),
                    display_name=expected_u,
                    role="principal",
                    is_active=True,
                )
                db.add(row)
            else:
                row.password_hash = hash_password(expected_p)
                row.role = "principal"
                row.is_active = True
            row.last_login_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return row
    return None
