"""Repair clients whose CSV/TSV fields were imported into the wrong columns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import Client
from app.services.import_csv import normalize_company_number


@dataclass
class RepairResult:
    repaired: int = 0
    skipped: int = 0
    deleted_stubs: int = 0
    errors: List[str] = field(default_factory=list)


def _looks_mangled(client: Client) -> bool:
    name = client.company_name or ""
    number = client.company_number or ""
    return "\t" in name or "\t" in number


def _is_company_number(token: str) -> bool:
    if not token:
        return False
    cleaned = re.sub(r"\s+", "", token.upper())
    return bool(re.fullmatch(r"(\d{6,8}|[A-Z]{2}\d{6})", cleaned))


def _is_email(token: str) -> bool:
    return bool(token and "@" in token and "." in token)


def _is_postcode(token: str) -> bool:
    if not token:
        return False
    return bool(
        re.fullmatch(
            r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}",
            token.upper().strip(),
        )
    )


def _is_stub_row(client: Client) -> bool:
    """True if row is essentially just a company number with no real name."""
    name = (client.company_name or "").strip()
    number = (client.company_number or "").strip()
    if not name and number:
        return True
    if name and number and normalize_company_number(name) == normalize_company_number(
        number
    ):
        return True
    if name and _is_company_number(name) and (not number or number == name):
        return True
    return False


def parse_mangled_client(client: Client) -> Optional[dict]:
    """
    Rebuild fields from tab-glued company_name + company_number.

    Typical original layout (positional):
    company_name, company_number, contact, email, phone,
    address1, address2, town, [area], postcode, client_type, status, vat, utr
    """
    name = client.company_name or ""
    number = client.company_number or ""

    if "\t" not in name and "\t" not in number:
        return None

    left = name.split("\t") if "\t" in name else ([name] if name else [])
    right = number.split("\t") if "\t" in number else ([number] if number else [])
    parts = [p.strip() for p in (left + right)]

    # Drop trailing empty strings
    while parts and parts[-1] == "":
        parts.pop()

    if len(parts) < 2:
        return None

    company_name = parts[0] or None
    company_number = None

    # Prefer explicit second field if it looks like a company number
    if len(parts) > 1 and _is_company_number(parts[1]):
        company_number = normalize_company_number(parts[1])
        rest = parts[2:]
    else:
        # Scan for company number
        rest = []
        for i, p in enumerate(parts):
            if i == 0:
                continue
            if company_number is None and _is_company_number(p):
                company_number = normalize_company_number(p)
            else:
                rest.append(p)
        if company_number is None and len(parts) > 1:
            candidate = normalize_company_number(parts[1])
            if candidate and re.search(r"\d", candidate):
                company_number = candidate
                rest = parts[2:]

    if not company_number:
        return None

    # If company_name itself is just a number, keep scanning rest for a better name
    if company_name and _is_company_number(company_name):
        for p in rest:
            if p and not _is_email(p) and not _is_company_number(p) and not _is_postcode(p):
                if not re.fullmatch(r"[\d\s+().-]+", p):
                    company_name = p
                    break

    contact_name = None
    email = None
    phone = None
    address_line1 = None
    address_line2 = None
    town = None
    postcode = None
    client_type = None
    overall_status = "Active"
    vat_number = None
    utr = None

    # Positional parse of rest with light heuristics
    # rest: contact, email, phone, addr1, addr2, town, area?, postcode, type, status, vat, utr
    idx = 0
    n = len(rest)

    def take():
        nonlocal idx
        if idx >= n:
            return None
        val = rest[idx]
        idx += 1
        return val

    # contact (may be empty string already stripped)
    if idx < n and not _is_email(rest[idx]) and not re.fullmatch(r"[\d\s+().-]{7,}", rest[idx] or ""):
        contact_name = take() or None
        if contact_name == "":
            contact_name = None
    elif idx < n and rest[idx] == "":
        take()

    # email
    if idx < n and _is_email(rest[idx]):
        email = take()
    elif idx < n and "@" in (rest[idx] or ""):
        email = take()

    # phone
    if idx < n and re.search(r"\d{6,}", rest[idx] or ""):
        phone = take()

    # address lines until we hit postcode or type/status keywords
    type_tokens = (
        "limited company",
        "llp",
        "sole trader",
        "partnership",
        "plc",
        "ltd",
        "limited",
    )
    status_tokens = {"active", "inactive", "prospect", "former", "lead"}

    addr_bits: List[str] = []
    while idx < n:
        p = rest[idx]
        low = (p or "").lower().strip()
        if _is_postcode(p):
            postcode = take()
            break
        if low in status_tokens:
            break
        if any(t == low or t in low for t in type_tokens) and len(low) < 40:
            break
        if re.fullmatch(r"\d{9,12}", re.sub(r"\s", "", p or "")):
            break
        addr_bits.append(take() or "")

    if addr_bits:
        address_line1 = addr_bits[0] or None
        if len(addr_bits) > 1:
            address_line2 = addr_bits[1] or None
        if len(addr_bits) > 2:
            town = addr_bits[2] or None
        if len(addr_bits) > 3 and not postcode:
            # area + maybe postcode mixed in
            if _is_postcode(addr_bits[3]):
                postcode = addr_bits[3]
            elif len(addr_bits) > 4 and _is_postcode(addr_bits[4]):
                town = f"{addr_bits[2]}, {addr_bits[3]}".strip(", ")
                postcode = addr_bits[4]
            else:
                # Boothstown, Worsley style
                if not town:
                    town = addr_bits[2]
                elif addr_bits[3]:
                    town = f"{town}, {addr_bits[3]}"

    # type, status, vat, utr
    while idx < n:
        p = take()
        if not p:
            continue
        low = p.lower().strip()
        if low in status_tokens:
            overall_status = p.title() if low == "active" else p
            continue
        if any(t in low for t in type_tokens) and not client_type:
            client_type = p
            continue
        if _is_postcode(p) and not postcode:
            postcode = p
            continue
        digits = re.sub(r"\s", "", p)
        if re.fullmatch(r"\d{9,12}", digits):
            if not vat_number and len(digits) in (9, 12):
                vat_number = p
            elif not utr:
                utr = p
            else:
                pass
            continue
        if re.fullmatch(r"\d{10}", digits) and not utr:
            utr = p

    return {
        "company_name": company_name,
        "company_number": company_number,
        "contact_name": contact_name,
        "email": email,
        "phone": phone,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "town": town,
        "postcode": postcode,
        "client_type": client_type,
        "overall_status": overall_status or "Active",
        "vat_number": vat_number,
        "utr": utr,
    }


def apply_parsed(client: Client, data: dict) -> None:
    client.company_name = data["company_name"]
    client.company_number = data["company_number"]
    client.contact_name = data.get("contact_name")
    client.email = data.get("email")
    client.phone = data.get("phone")
    client.address_line1 = data.get("address_line1")
    client.address_line2 = data.get("address_line2")
    client.town = data.get("town")
    client.postcode = data.get("postcode")
    client.client_type = data.get("client_type")
    client.overall_status = data.get("overall_status") or "Active"
    client.vat_number = data.get("vat_number")
    client.utr = data.get("utr")
    client.source = client.source or "csv"


def repair_all_clients(db: Session) -> RepairResult:
    result = RepairResult()
    clients = db.query(Client).order_by(Client.id).all()

    # Pass 1: remove stub rows (name is just a company number) so real
    # repaired rows can claim those numbers without UNIQUE collisions.
    remaining = []
    for client in clients:
        if _is_stub_row(client) and not _looks_mangled(client):
            result.errors.append(
                f"Deleted stub client id={client.id} "
                f"({client.company_number})"
            )
            db.delete(client)
            result.deleted_stubs += 1
        else:
            remaining.append(client)
    db.flush()

    # Index company numbers still in use after stub cleanup
    claimed = {}
    for c in remaining:
        if c.company_number and not _looks_mangled(c):
            cn = normalize_company_number(c.company_number)
            if cn:
                claimed[cn] = c.id

    # Pass 2: repair mangled rows
    for client in remaining:
        if not _looks_mangled(client):
            result.skipped += 1
            continue

        data = parse_mangled_client(client)
        if not data:
            result.skipped += 1
            result.errors.append(f"Client id={client.id}: could not parse fields")
            continue

        cn = data["company_number"]
        owner = claimed.get(cn)
        if owner is not None and owner != client.id:
            result.skipped += 1
            result.errors.append(
                f"Client id={client.id}: company_number {cn} already used by "
                f"id={owner} — left unrepaired"
            )
            continue

        apply_parsed(client, data)
        claimed[cn] = client.id
        result.repaired += 1

    db.commit()
    return result
