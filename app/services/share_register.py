"""Share register: practice members list + seed from Companies House public data."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.share_register import ShareClass, Shareholding
from app.services.company_numbers import normalize_company_number
from app.services.secrets_crypto import decrypt_secret, encrypt_secret, mask_secret


def list_share_classes(db: Session, client_id: int) -> List[ShareClass]:
    return (
        db.query(ShareClass)
        .filter(ShareClass.client_id == client_id)
        .order_by(ShareClass.sort_order, ShareClass.id)
        .all()
    )


def list_holdings(db: Session, client_id: int) -> List[Shareholding]:
    return (
        db.query(Shareholding)
        .filter(Shareholding.client_id == client_id)
        .order_by(Shareholding.sort_order, Shareholding.id)
        .all()
    )


def set_ch_auth_code(db: Session, client: Client, plain: str) -> Client:
    """Store company authentication code encrypted."""
    plain = (plain or "").strip()
    if not plain:
        client.ch_authentication_code = None
    elif plain.startswith("•") or plain == mask_secret(client.ch_authentication_code):
        # UI submitted mask — leave unchanged
        pass
    else:
        client.ch_authentication_code = encrypt_secret(plain)
    client.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(client)
    return client


def ch_auth_code_plain(client: Client) -> Optional[str]:
    return decrypt_secret(client.ch_authentication_code)


def ch_auth_code_masked(client: Client) -> str:
    return mask_secret(client.ch_authentication_code) or ""


def has_ch_auth_code(client: Client) -> bool:
    return bool(decrypt_secret(client.ch_authentication_code))


def add_share_class(
    db: Session,
    client_id: int,
    *,
    name: str = "Ordinary",
    nominal_value: float = 1.0,
    currency: str = "GBP",
    aggregate_shares: Optional[float] = None,
    rights_notes: str = "",
    source: str = "manual",
) -> ShareClass:
    sc = ShareClass(
        client_id=client_id,
        name=(name or "Ordinary").strip() or "Ordinary",
        nominal_value=float(nominal_value or 1),
        currency=(currency or "GBP").strip() or "GBP",
        aggregate_shares=float(aggregate_shares) if aggregate_shares not in (None, "") else None,
        rights_notes=(rights_notes or "").strip() or None,
        source=source or "manual",
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)
    return sc


def add_holding(
    db: Session,
    client_id: int,
    *,
    member_name: str,
    shares: Optional[float] = None,
    share_class_id: Optional[int] = None,
    person_id: Optional[int] = None,
    member_type: str = "individual",
    company_number: str = "",
    psc_natures: str = "",
    certificate_no: str = "",
    date_acquired: Optional[date] = None,
    notes: str = "",
    source: str = "manual",
    status: str = "draft",
    is_director: bool = False,
    is_psc: bool = False,
    commit: bool = True,
) -> Shareholding:
    h = Shareholding(
        client_id=client_id,
        share_class_id=share_class_id,
        person_id=person_id,
        member_name=(member_name or "").strip() or "Unknown member",
        member_type=(member_type or "individual").strip().lower() or "individual",
        company_number=normalize_company_number(company_number) if company_number else None,
        shares=float(shares) if shares not in (None, "") else None,
        psc_natures=(psc_natures or "").strip() or None,
        certificate_no=(certificate_no or "").strip() or None,
        date_acquired=date_acquired,
        notes=(notes or "").strip() or None,
        source=source or "manual",
        status=status or "draft",
        is_director=bool(is_director),
        is_psc=bool(is_psc),
    )
    db.add(h)
    if commit:
        db.commit()
        db.refresh(h)
    else:
        db.flush()
    return h


def _norm_person_key(name: str) -> str:
    """Dedupe key: 'GREENE, Mark' / 'Mr Mark Greene' → 'mark greene'."""
    s = (name or "").strip().lower()
    if not s:
        return ""
    if "," in s:
        last, first = [x.strip() for x in s.split(",", 1)]
        s = f"{first} {last}"
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for t in ("mr ", "mrs ", "ms ", "miss ", "dr ", "sir "):
        if s.startswith(t):
            s = s[len(t) :].strip()
    return s


def is_shareholder_row(h: Shareholding) -> bool:
    """True once practice has allocated a positive share count."""
    try:
        return h.shares is not None and float(h.shares) > 0
    except (TypeError, ValueError):
        return False


def delete_holding(db: Session, holding_id: int, client_id: int) -> bool:
    h = (
        db.query(Shareholding)
        .filter(Shareholding.id == holding_id, Shareholding.client_id == client_id)
        .first()
    )
    if not h:
        return False
    db.delete(h)
    db.commit()
    return True


def delete_share_class(db: Session, class_id: int, client_id: int) -> bool:
    sc = (
        db.query(ShareClass)
        .filter(ShareClass.id == class_id, ShareClass.client_id == client_id)
        .first()
    )
    if not sc:
        return False
    db.delete(sc)
    db.commit()
    return True


def mark_register_verified(
    db: Session, client: Client, *, by: str = "practice"
) -> Client:
    client.share_register_verified_at = datetime.utcnow()
    client.share_register_verified_by = (by or "practice").strip()[:120]
    client.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(client)
    return client


def _parse_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").replace("£", "").strip()
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _capital_from_filings(items: List[dict]) -> List[Dict[str, Any]]:
    """Extract capital class hints from filing history description_values."""
    found: List[Dict[str, Any]] = []
    for it in items or []:
        desc = (it.get("description") or "").lower()
        vals = it.get("description_values") or {}
        capital = vals.get("capital")
        if isinstance(capital, list):
            for c in capital:
                if not isinstance(c, dict):
                    continue
                found.append(
                    {
                        "figure": c.get("figure"),
                        "currency": c.get("currency") or "GBP",
                        "description": it.get("description"),
                        "date": it.get("date") or it.get("action_date"),
                        "type": it.get("type"),
                    }
                )
        # Also look for figure in description_values
        if "capital" in desc or (it.get("type") or "").upper() in (
            "SH01",
            "CS01",
            "NEWINC",
        ):
            fig = vals.get("capital") or vals.get("figure")
            if fig and not isinstance(fig, list):
                found.append(
                    {
                        "figure": fig,
                        "currency": vals.get("currency") or "GBP",
                        "description": it.get("description"),
                        "date": it.get("date"),
                        "type": it.get("type"),
                    }
                )
    return found


def _format_psc_name(item: dict) -> str:
    name = (item.get("name") or "").strip()
    if name:
        if name.isupper() or ("," in name and name.split(",")[0].isupper()):
            if "," in name:
                last, first = [x.strip() for x in name.split(",", 1)]
                return f"{first.title()} {last.title()}"
            return name.title()
        return name
    idata = item.get("identification") or {}
    if idata.get("legal_name"):
        return str(idata["legal_name"]).title()
    return "PSC (unnamed)"


def seed_register_from_ch(
    db: Session,
    client: Client,
    *,
    replace_draft: bool = True,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Pull CH public data and seed share classes + potential shareholders.

    Seeds active **directors** and **PSCs** as draft potential members (deduped —
    same person who is director + PSC is one row with both flags).

    Exact share numbers are left blank for you to allocate (simple for most clients).
    """
    from app.services.companies_house import (
        download_cs_bundle,
        fetch_filing_history,
        has_api_key,
    )

    cn = normalize_company_number(client.company_number or "")
    if not cn or cn.startswith("IND-") or cn.startswith("PENDING"):
        return False, "Company number required (not for individual clients)", {}
    if not has_api_key():
        return False, "Companies House API key not configured", {}

    bundle_res = download_cs_bundle(cn)
    if not bundle_res.ok:
        return False, bundle_res.error or "CH download failed", {}

    bundle = bundle_res.profile or {}
    officers = (bundle.get("officers") or {}).get("items") or []
    pscs = (bundle.get("pscs") or {}).get("items") or []

    hist = fetch_filing_history(cn, category="capital", items_per_page=50)
    capital_items = (hist.profile or {}).get("items") or [] if hist.ok else []
    hist2 = fetch_filing_history(cn, items_per_page=30)
    extra = []
    if hist2.ok:
        for it in (hist2.profile or {}).get("items") or []:
            t = (it.get("type") or "").upper()
            if t in ("CS01", "NEWINC", "SH01", "SH02", "SH19"):
                extra.append(it)
    capital_hints = _capital_from_filings(capital_items + extra)

    stats = {
        "directors": 0,
        "pscs": 0,
        "capital_hints": len(capital_hints),
        "classes_added": 0,
        "holdings_added": 0,
        "holdings_updated": 0,
        "holdings_cleared": 0,
    }

    if replace_draft:
        for h in list_holdings(db, client.id):
            # Keep rows with allocated shares or manual source
            if is_shareholder_row(h) or (h.source or "") == "manual":
                continue
            if (h.source or "").startswith("ch_") and (h.status or "") == "draft":
                db.delete(h)
                stats["holdings_cleared"] += 1
        db.commit()

    existing_classes = list_share_classes(db, client.id)
    ordinary = None
    for sc in existing_classes:
        if (sc.name or "").lower() in ("ordinary", "ordinary shares", "ord"):
            ordinary = sc
            break

    if not ordinary:
        agg = None
        cur = "GBP"
        for h in capital_hints:
            f = _parse_float(h.get("figure"))
            if f and f > 0:
                agg = f
                cur = h.get("currency") or "GBP"
                break
        ordinary = add_share_class(
            db,
            client.id,
            name="Ordinary",
            nominal_value=1.0,
            currency=cur,
            aggregate_shares=agg,
            rights_notes="Seeded from Companies House capital filings / defaults",
            source="ch_capital",
        )
        stats["classes_added"] += 1
    elif ordinary.aggregate_shares is None and capital_hints:
        for h in capital_hints:
            f = _parse_float(h.get("figure"))
            if f and f > 0:
                ordinary.aggregate_shares = f
                ordinary.currency = h.get("currency") or ordinary.currency or "GBP"
                ordinary.updated_at = datetime.utcnow()
                db.commit()
                break

    # Build merged people map by normalised name
    # { key: {name, is_director, is_psc, role, natures, member_type, company_number} }
    people_map: Dict[str, Dict[str, Any]] = {}

    for o in officers:
        if o.get("resigned_on"):
            continue
        role = (o.get("officer_role") or "").strip().lower()
        if "director" not in role and role not in ("llp-member", "member"):
            continue
        stats["directors"] += 1
        name = _format_psc_name(o)  # same name tidy
        key = _norm_person_key(name)
        if not key:
            continue
        rec = people_map.setdefault(
            key,
            {
                "name": name,
                "is_director": False,
                "is_psc": False,
                "roles": [],
                "natures": [],
                "member_type": "individual",
                "company_number": None,
            },
        )
        rec["is_director"] = True
        rec["roles"].append(o.get("officer_role") or "director")
        if name and len(name) > len(rec["name"] or ""):
            rec["name"] = name

    for item in pscs:
        if item.get("ceased_on"):
            continue
        kind = (item.get("kind") or "").lower()
        if "statement" in kind and "person" not in kind:
            continue
        if kind.endswith("statement"):
            continue
        stats["pscs"] += 1
        name = _format_psc_name(item)
        key = _norm_person_key(name)
        if not key:
            continue
        natures = item.get("natures_of_control") or []
        if isinstance(natures, list):
            natures_s = "; ".join(str(n).replace("-", " ") for n in natures)
        else:
            natures_s = str(natures or "")
        mtype = (
            "corporate"
            if "corporate" in kind or item.get("identification")
            else "individual"
        )
        cn_member = None
        if isinstance(item.get("identification"), dict):
            cn_member = item["identification"].get("registration_number")
        rec = people_map.setdefault(
            key,
            {
                "name": name,
                "is_director": False,
                "is_psc": False,
                "roles": [],
                "natures": [],
                "member_type": mtype,
                "company_number": cn_member,
            },
        )
        rec["is_psc"] = True
        if natures_s:
            rec["natures"].append(natures_s)
        if mtype == "corporate":
            rec["member_type"] = "corporate"
        if cn_member:
            rec["company_number"] = cn_member

    # Index existing holdings (keep manual / share-allocated rows)
    existing_by_key: Dict[str, Shareholding] = {}
    for h in list_holdings(db, client.id):
        existing_by_key[_norm_person_key(h.member_name or "")] = h

    for key, rec in people_map.items():
        if not key:
            continue
        existing = existing_by_key.get(key)
        natures_s = "; ".join(rec["natures"]) if rec["natures"] else ""
        roles_s = ", ".join(rec["roles"]) if rec["roles"] else ""
        if existing:
            # Don't wipe allocated shares; still refresh flags
            changed = False
            if rec["is_director"] and not existing.is_director:
                existing.is_director = True
                changed = True
            if rec["is_psc"] and not existing.is_psc:
                existing.is_psc = True
                changed = True
            if natures_s and not (existing.psc_natures or "").strip():
                existing.psc_natures = natures_s
                changed = True
            if changed:
                existing.updated_at = datetime.utcnow()
                stats["holdings_updated"] += 1
            continue

        src = "ch_both" if rec["is_director"] and rec["is_psc"] else (
            "ch_director" if rec["is_director"] else "ch_psc"
        )
        note_bits = [
            "Potential shareholder from Companies House (director/PSC).",
            "Delete if not a member. Set exact shares when known.",
        ]
        if roles_s:
            note_bits.append(f"Officer: {roles_s}.")
        if natures_s:
            note_bits.append(f"PSC: {natures_s}.")
        add_holding(
            db,
            client.id,
            member_name=rec["name"],
            shares=None,
            share_class_id=ordinary.id if ordinary else None,
            member_type=rec["member_type"],
            company_number=rec.get("company_number") or "",
            psc_natures=natures_s,
            notes=" ".join(note_bits),
            source=src,
            status="draft",
            is_director=bool(rec["is_director"]),
            is_psc=bool(rec["is_psc"]),
            commit=False,
        )
        stats["holdings_added"] += 1

    client.ch_register_seeded_at = datetime.utcnow()
    client.share_register_notes = (
        f"Last CH seed {client.ch_register_seeded_at.date().isoformat()}. "
        f"Directors: {stats['directors']}; PSCs: {stats['pscs']}; "
        f"capital hints: {stats['capital_hints']}. "
        "Allocate share numbers; delete anyone who is not a shareholder."
    )
    client.updated_at = datetime.utcnow()
    db.commit()

    msg = (
        f"CH seed: {stats['holdings_added']} potential shareholder(s) "
        f"({stats['directors']} directors, {stats['pscs']} PSCs — de-duplicated)"
        + (f", {stats['holdings_updated']} updated" if stats["holdings_updated"] else "")
        + (f", cleared {stats['holdings_cleared']} old draft rows" if stats["holdings_cleared"] else "")
        + ". Set share counts; delete non-shareholders."
    )
    return True, msg, stats


def seed_all_clients_from_ch(
    db: Session,
    *,
    force: bool = False,
    limit: int = 400,
) -> Dict[str, Any]:
    """Seed share registers for active limited companies."""
    q = (
        db.query(Client)
        .filter(Client.overall_status == "Active")
        .order_by(Client.company_name)
    )
    ok_n = skip_n = err_n = 0
    errors: List[str] = []
    for client in q.limit(limit).all():
        cn = (client.company_number or "").strip().upper()
        if not cn or cn.startswith("IND-") or cn.startswith("PENDING"):
            skip_n += 1
            continue
        if not force and client.ch_register_seeded_at:
            # Skip recently seeded unless empty holdings
            if list_holdings(db, client.id):
                skip_n += 1
                continue
        try:
            ok, msg, _ = seed_register_from_ch(db, client, replace_draft=True)
            if ok:
                ok_n += 1
            else:
                err_n += 1
                errors.append(f"{client.display_name()}: {msg}")
        except Exception as exc:
            err_n += 1
            errors.append(f"{client.display_name()}: {exc}")
            db.rollback()
    return {
        "ok": ok_n,
        "skipped": skip_n,
        "errors": err_n,
        "error_samples": errors[:15],
    }


def contact_role_rows(
    db: Session,
    client_id: int,
    people: List[Any],
) -> List[Dict[str, Any]]:
    """
    Rows for contacts table with Director / Shareholder / PSC / Other ticks.

    Merges CRM people with CH-seeded shareholdings (by normalised name).
    """
    holdings = list_holdings(db, client_id)
    by_key: Dict[str, Dict[str, Any]] = {}

    def ensure(key: str, display: str) -> Dict[str, Any]:
        if key not in by_key:
            by_key[key] = {
                "key": key,
                "name": display,
                "person": None,
                "holding": None,
                "email": None,
                "phone": None,
                "is_director": False,
                "is_shareholder": False,
                "is_psc": False,
                "is_other": False,
            }
        return by_key[key]

    for h in holdings:
        key = _norm_person_key(h.member_name or "")
        if not key:
            continue
        row = ensure(key, h.member_name)
        row["holding"] = h
        row["is_director"] = row["is_director"] or bool(h.is_director)
        row["is_psc"] = row["is_psc"] or bool(h.is_psc)
        row["is_shareholder"] = row["is_shareholder"] or is_shareholder_row(h)

    for p in people or []:
        name = p.display_name() if hasattr(p, "display_name") else (p.full_name or "")
        key = _norm_person_key(name)
        if not key:
            key = f"person-{p.id}"
        row = ensure(key, name)
        row["person"] = p
        row["email"] = p.email
        row["phone"] = p.phone
        role = (getattr(p, "role", None) or "").lower()
        if "director" in role:
            row["is_director"] = True
        if "shareholder" in role or "member" in role:
            row["is_shareholder"] = True
        if "psc" in role or "significant control" in role:
            row["is_psc"] = True

    rows = []
    for row in by_key.values():
        if not (row["is_director"] or row["is_shareholder"] or row["is_psc"]):
            row["is_other"] = True
        rows.append(row)
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def register_summary(db: Session, client_id: int) -> Dict[str, Any]:
    classes = list_share_classes(db, client_id)
    holdings = list_holdings(db, client_id)
    total_shares = sum(float(h.shares or 0) for h in holdings)
    return {
        "class_count": len(classes),
        "holding_count": len(holdings),
        "total_shares_known": total_shares,
        "draft_count": sum(1 for h in holdings if (h.status or "") == "draft"),
        "verified_count": sum(1 for h in holdings if (h.status or "") == "verified"),
    }
