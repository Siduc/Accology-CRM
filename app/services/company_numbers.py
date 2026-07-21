"""UK company number normalisation (leading zeros, Excel float junk)."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models import Client


def normalize_company_number(value: Optional[str]) -> Optional[str]:
    """
    Clean and pad a Companies House number.

    - Strip spaces, upper-case
    - Excel floats like 8056337.0 → 8056337
    - Pure digits shorter than 8 → zero-pad to 8 (08056337)
    - Prefixed forms (SC/NI/OC/…) with short numeric part → pad digits to 6
    """
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", str(value).strip().upper())
    if not cleaned:
        return None

    # Excel / float paste
    if re.fullmatch(r"\d+\.0+", cleaned):
        cleaned = cleaned.split(".")[0]

    # Individual pseudo-numbers stay as-is
    if cleaned.startswith("IND-"):
        return cleaned

    # Pure numeric English/Welsh company numbers → 8 digits
    if cleaned.isdigit():
        if len(cleaned) < 8:
            return cleaned.zfill(8)
        return cleaned

    # Prefixed (Scotland SC, NI, LLP OC, etc.): usually 2 letters + 6 digits
    m = re.fullmatch(r"([A-Z]{1,2})(\d+)", cleaned)
    if m:
        prefix, digits = m.group(1), m.group(2)
        # Most CH prefixes use 6-digit serials
        width = 6 if len(prefix) == 2 else 6
        return prefix + digits.zfill(width)

    return cleaned


def pad_all_client_company_numbers(db: Session) -> Tuple[int, int, list]:
    """
    Update clients whose numbers need leading zeros.
    Returns (updated, unchanged, errors).
    """
    updated = 0
    unchanged = 0
    errors: list = []

    clients = db.query(Client).order_by(Client.id).all()
    # Map normalized → list of clients currently holding that number after change
    claimed = {}
    for c in clients:
        current = (c.company_number or "").strip()
        if current and not current.upper().startswith("IND-"):
            claimed.setdefault(current.upper().replace(" ", ""), c.id)

    for client in clients:
        old = client.company_number
        if not old or str(old).upper().startswith("IND-"):
            unchanged += 1
            continue

        new = normalize_company_number(old)
        if not new or new == old or new == (old or "").strip().upper().replace(" ", ""):
            # still check zfill difference vs stripped old
            stripped = re.sub(r"\s+", "", str(old).strip().upper())
            if new == stripped:
                unchanged += 1
                continue

        # Collision?
        owner = claimed.get(new)
        if owner is not None and owner != client.id:
            errors.append(
                f"Client #{client.id} ({old} → {new}): number already used by client #{owner}"
            )
            unchanged += 1
            continue

        # free old claim
        old_key = re.sub(r"\s+", "", str(old).strip().upper())
        if claimed.get(old_key) == client.id:
            del claimed[old_key]

        client.company_number = new
        claimed[new] = client.id
        updated += 1

    if updated:
        db.commit()
    return updated, unchanged, errors
