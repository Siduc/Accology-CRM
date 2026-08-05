"""Share register: practice members list + seed from Companies House public data."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.person import Person
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


def ensure_person_for_member(
    db: Session,
    client: Client,
    member_name: str,
    *,
    role_hint: str = "",
    is_director: bool = False,
    is_psc: bool = False,
    is_shareholder: bool = False,
    notes: str = "",
) -> Optional[Person]:
    """
    Find or create a CRM Person for a shareholding / CH officer name.

    Cross-references the whole practice directory (first+last / fuzzy name match)
    so e.g. CH "Philip James Davies" links to existing "Mr Philip Davies".
    Always ensures the person is linked to this client.
    """
    from app.services.cs_automation import (
        _officer_display_name,
        find_people_matching_officer,
    )
    from app.text_format import normalize_person_name

    raw = (member_name or "").strip()
    if not raw or raw.lower() in ("unknown member", "—", "-"):
        return None

    display = normalize_person_name(_officer_display_name(raw) or raw) or raw

    # 1) Prefer person already on this client
    client_people = list(client.people or [])
    local_hits = find_people_matching_officer(client_people, raw)
    if not local_hits:
        local_hits = find_people_matching_officer(client_people, display)

    person: Optional[Person] = None
    if local_hits:
        person = _prefer_richest_person(local_hits)
    else:
        # 2) Whole practice directory
        directory = db.query(Person).order_by(Person.id).all()
        hits = find_people_matching_officer(directory, raw)
        if not hits:
            hits = find_people_matching_officer(directory, display)
        if hits:
            person = _prefer_richest_person(hits)

    role = (role_hint or "").strip()
    if not role:
        bits = []
        if is_director:
            bits.append("Director")
        if is_psc:
            bits.append("PSC")
        if is_shareholder:
            bits.append("Shareholder")
        role = " / ".join(bits) if bits else "Contact"

    if person:
        if client not in (person.clients or []):
            person.clients.append(client)
        # Enrich blank fields only
        if role and not (person.role or "").strip():
            person.role = role
        # Prefer fuller legal-style name from CH when CRM is short
        if display and person.full_name:
            if len(display.split()) > len((person.full_name or "").split()) and _norm_person_key(
                display
            ) == _norm_person_key(person.full_name):
                # Keep CRM preferred name with title if present; don't overwrite rich CRM
                pass
        db.flush()
        return person

    # 3) Create new person
    person = Person(
        full_name=display,
        role=role,
        person_status="Contact",
        is_primary=False,
        notes=(notes or "").strip()
        or f"Created from share register / Companies House ({raw})",
    )
    person.clients.append(client)
    db.add(person)
    db.flush()
    return person


def _prefer_richest_person(people: List[Person]) -> Person:
    """Prefer person with email/phone/more company links (keep detailed records)."""

    def score(p: Person) -> tuple:
        return (
            1 if (p.email or "").strip() else 0,
            1 if (p.phone or "").strip() else 0,
            1 if (p.ch_code or "").strip() else 0,
            1 if (p.notes or "").strip() else 0,
            len(getattr(p, "clients", None) or []),
            -(p.id or 0),  # older id first when tied
        )

    return sorted(people, key=score, reverse=True)[0]


def link_holding_to_person(
    db: Session,
    client: Client,
    holding: Shareholding,
    *,
    commit: bool = False,
) -> Optional[Person]:
    """Ensure holding.person_id points at a CRM person (find/create + link client)."""
    if holding.person_id:
        person = db.query(Person).filter(Person.id == holding.person_id).first()
        if person:
            if client not in (person.clients or []):
                person.clients.append(client)
                db.flush()
            return person
    person = ensure_person_for_member(
        db,
        client,
        holding.member_name or "",
        is_director=bool(holding.is_director),
        is_psc=bool(holding.is_psc),
        is_shareholder=is_shareholder_row(holding),
        notes=(holding.notes or "")[:200],
    )
    if person:
        holding.person_id = person.id
        # Align member_name display if empty-ish
        if not (holding.member_name or "").strip():
            holding.member_name = person.full_name or holding.member_name
        holding.updated_at = datetime.utcnow()
        db.flush()
    if commit:
        db.commit()
    return person


def sync_holdings_to_people(
    db: Session,
    client: Client,
    *,
    commit: bool = True,
) -> Dict[str, int]:
    """Link every shareholding on the client to a people-table row (no duplicates)."""
    linked = created_proxy = 0
    for h in list_holdings(db, client.id):
        before = h.person_id
        person = link_holding_to_person(db, client, h, commit=False)
        if person:
            linked += 1
            if before is None:
                created_proxy += 1
    if commit:
        db.commit()
    return {"linked": linked, "new_or_matched": created_proxy}


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
    ensure_person: bool = True,
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
    db.flush()
    if ensure_person and not h.person_id:
        client = db.query(Client).filter(Client.id == client_id).first()
        if client:
            link_holding_to_person(db, client, h, commit=False)
    if commit:
        db.commit()
        db.refresh(h)
    else:
        db.flush()
    return h


def _norm_person_key(name: str) -> str:
    """Dedupe key: first+last so 'Philip James Davies' matches 'Philip Davies'."""
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
    tokens = [t for t in s.split() if t]
    if len(tokens) >= 2:
        return f"{tokens[0]} {tokens[-1]}"
    return " ".join(tokens)


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


def update_share_class_aggregate(
    db: Session,
    class_id: int,
    client_id: int,
    aggregate_shares: Optional[float],
) -> Optional[ShareClass]:
    """Set issued (aggregate) shares for a class — used when CH seed missed capital."""
    sc = (
        db.query(ShareClass)
        .filter(ShareClass.id == class_id, ShareClass.client_id == client_id)
        .first()
    )
    if not sc:
        return None
    sc.aggregate_shares = (
        float(aggregate_shares) if aggregate_shares is not None else None
    )
    sc.updated_at = datetime.utcnow()
    if (sc.source or "") == "ch_capital":
        sc.source = "ch_capital+manual"
    db.commit()
    db.refresh(sc)
    return sc


def allocation_check(
    db: Session,
    client_id: int,
    holding: Shareholding,
    new_shares: Optional[float],
) -> Dict[str, Any]:
    """
    Check whether allocating new_shares would over-allocate the class pool.

    Returns ok / over / unknown_issued with remaining, issued, other_alloc, total.
    """
    summary = register_summary(db, client_id)
    issued = summary.get("issued")
    # Prefer class-specific issued when holding has a class
    if holding.share_class_id:
        sc = (
            db.query(ShareClass)
            .filter(
                ShareClass.id == holding.share_class_id,
                ShareClass.client_id == client_id,
            )
            .first()
        )
        if sc and sc.aggregate_shares is not None:
            issued = float(sc.aggregate_shares)

    other_alloc = sum(
        float(x.shares or 0)
        for x in list_holdings(db, client_id)
        if x.id != holding.id
        and (
            (holding.share_class_id and x.share_class_id == holding.share_class_id)
            or (not holding.share_class_id)
        )
        and x.shares is not None
    )
    proposed = float(new_shares) if new_shares is not None else 0.0
    total = other_alloc + proposed

    if issued is None:
        return {
            "status": "unknown_issued",
            "ok": True,  # allow save, but UI should warn
            "issued": None,
            "other_alloc": other_alloc,
            "proposed": proposed,
            "total": total,
            "remaining": None,
            "over_by": None,
        }

    issued_f = float(issued)
    remaining = issued_f - other_alloc
    over = total > issued_f + 0.0001
    return {
        "status": "over" if over else "ok",
        "ok": not over,
        "issued": issued_f,
        "other_alloc": other_alloc,
        "proposed": proposed,
        "total": total,
        "remaining": remaining,
        "over_by": (total - issued_f) if over else 0.0,
    }


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
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("£", "")
    if not s:
        return None
    # "90.00" or "90 ordinary shares"
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _capital_from_one_item(it: dict) -> List[Dict[str, Any]]:
    """Extract capital figures from one filing (or associated sub-filing)."""
    found: List[Dict[str, Any]] = []
    if not isinstance(it, dict):
        return found
    desc = (it.get("description") or "").lower()
    vals = it.get("description_values") or {}
    if not isinstance(vals, dict):
        vals = {}
    capital = vals.get("capital")
    ftype = (it.get("type") or "").upper()
    if isinstance(capital, list):
        for c in capital:
            if not isinstance(c, dict):
                continue
            found.append(
                {
                    "figure": c.get("figure"),
                    "currency": c.get("currency") or "GBP",
                    "description": it.get("description"),
                    "date": it.get("date") or it.get("action_date") or vals.get("date"),
                    "type": ftype or it.get("type"),
                }
            )
    # Scalar capital / figure on capital-related filings
    if "capital" in desc or "statement-of-capital" in desc or ftype in (
        "SH01",
        "SH02",
        "SH19",
        "CS01",
        "NEWINC",
    ):
        fig = vals.get("figure")
        if fig is None and capital is not None and not isinstance(capital, list):
            fig = capital
        if fig is not None and not isinstance(fig, (list, dict)):
            found.append(
                {
                    "figure": fig,
                    "currency": vals.get("currency") or "GBP",
                    "description": it.get("description"),
                    "date": it.get("date") or it.get("action_date"),
                    "type": ftype or it.get("type"),
                }
            )
    return found


def _capital_from_filings(items: List[dict]) -> List[Dict[str, Any]]:
    """
    Extract capital class hints from filing history.

    Important: incorporation (NEWINC) often nests the statement of capital under
    ``associated_filings`` (e.g. SH01 with capital figure 90) — not as a top-level
    capital-category filing. We must walk those nested items.
    """
    found: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        found.extend(_capital_from_one_item(it))
        for af in it.get("associated_filings") or []:
            found.extend(_capital_from_one_item(af))
    return found


def _best_capital_figure(hints: List[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    """
    Pick the best aggregate share count from capital hints.

    Prefers statement-of-capital / SH01 figures; skips tiny nonsense if larger
    values exist. Returns (figure, currency).
    """
    candidates: List[Tuple[float, str, int]] = []
    for h in hints or []:
        f = _parse_float(h.get("figure"))
        if not f or f <= 0:
            continue
        desc = (h.get("description") or "").lower()
        ftype = (h.get("type") or "").upper()
        # Prefer explicit capital statements and allotments
        rank = 0
        if "statement-of-capital" in desc or ftype == "SH01":
            rank = 3
        elif ftype in ("SH02", "SH19", "NEWINC"):
            rank = 2
        elif "capital" in desc:
            rank = 1
        cur = (h.get("currency") or "GBP") or "GBP"
        candidates.append((f, cur, rank))
    if not candidates:
        return None, "GBP"
    # Highest rank, then largest figure (issued capital usually the full pool)
    candidates.sort(key=lambda x: (x[2], x[0]), reverse=True)
    return candidates[0][0], candidates[0][1]


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

    Seeds **all** directors (including resigned) and **all** PSCs (including ceased)
    as draft potential members — useful for silent / retiring partners who may still
    hold shares. Delete anyone who is not a member. Deduped by name; same person who
    is director + PSC is one row with both flags.

    Exact share numbers are left blank for you to allocate.
    """
    from app.services.companies_house import (
        download_cs_bundle,
        fetch_filing_history,
        has_api_key,
    )

    if not client_is_ch_entity(client):
        return (
            False,
            "Not a Companies House entity (sole trader / partnership / no valid company number) — CH work skipped.",
            {},
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

    # Full filing history (capital often nested under NEWINC associated_filings)
    hist_all = fetch_filing_history(cn, items_per_page=100)
    all_items = (hist_all.profile or {}).get("items") or [] if hist_all.ok else []
    hist_cap = fetch_filing_history(cn, category="capital", items_per_page=50)
    capital_items = (hist_cap.profile or {}).get("items") or [] if hist_cap.ok else []
    capital_hints = _capital_from_filings(list(capital_items) + list(all_items))
    agg_best, cur_best = _best_capital_figure(capital_hints)

    stats = {
        "directors": 0,
        "directors_resigned": 0,
        "pscs": 0,
        "pscs_ceased": 0,
        "capital_hints": len(capital_hints),
        "capital_figure": agg_best,
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
        ordinary = add_share_class(
            db,
            client.id,
            name="Ordinary",
            nominal_value=1.0,
            currency=cur_best or "GBP",
            aggregate_shares=agg_best,
            rights_notes=(
                f"Seeded from Companies House capital filings"
                + (f" (issued {agg_best:g})" if agg_best else " — issued unknown, set manually")
            ),
            source="ch_capital",
        )
        stats["classes_added"] += 1
    elif agg_best and (
        ordinary.aggregate_shares is None
        or (ordinary.source or "") in ("ch_capital", "ch_filing")
    ):
        # Refresh CH-sourced issued capital (e.g. was blank; now found 90 on NEWINC)
        prev = ordinary.aggregate_shares
        ordinary.aggregate_shares = agg_best
        ordinary.currency = cur_best or ordinary.currency or "GBP"
        ordinary.rights_notes = (
            f"Seeded from Companies House capital filings (issued {agg_best:g})"
            + (f" — was {prev:g}" if prev is not None and float(prev) != float(agg_best) else "")
        )
        ordinary.updated_at = datetime.utcnow()
        db.commit()

    # Build merged people map by normalised name — include ALL officers/PSCs
    # (active + resigned/ceased) so silent/retiring partners can be allocated or deleted.
    people_map: Dict[str, Dict[str, Any]] = {}

    def _blank_rec(name: str, **kwargs: Any) -> Dict[str, Any]:
        base = {
            "name": name,
            "is_director": False,
            "is_psc": False,
            "resigned_director": False,
            "ceased_psc": False,
            "roles": [],
            "natures": [],
            "member_type": "individual",
            "company_number": None,
            "resigned_on": None,
            "ceased_on": None,
        }
        base.update(kwargs)
        return base

    for o in officers:
        role = (o.get("officer_role") or "").strip().lower()
        # Directors / LLP members (active or resigned) — skip pure secretaries etc.
        if "director" not in role and role not in (
            "llp-member",
            "member",
            "corporate-director",
            "corporate-llp-member",
        ):
            # Still include corporate-nominee-director style roles that contain director
            if "director" not in role and "member" not in role:
                continue
        resigned = bool(o.get("resigned_on"))
        if resigned:
            stats["directors_resigned"] += 1
        else:
            stats["directors"] += 1
        name = _format_psc_name(o)
        key = _norm_person_key(name)
        if not key:
            continue
        rec = people_map.setdefault(key, _blank_rec(name))
        if resigned:
            rec["resigned_director"] = True
            rec["resigned_on"] = str(o.get("resigned_on") or "")[:10]
            # Only mark is_director if still active (or never resigned)
            if not rec["is_director"]:
                pass
        else:
            rec["is_director"] = True
            rec["resigned_director"] = False
        role_label = (o.get("officer_role") or "director").replace("-", " ")
        if resigned:
            rec["roles"].append(f"{role_label} (resigned {rec['resigned_on']})")
        else:
            rec["roles"].append(role_label)
        if name and len(name) > len(rec["name"] or ""):
            rec["name"] = name

    for item in pscs:
        kind = (item.get("kind") or "").lower()
        if "statement" in kind and "person" not in kind:
            continue
        if kind.endswith("statement"):
            continue
        ceased = bool(item.get("ceased_on"))
        if ceased:
            stats["pscs_ceased"] += 1
        else:
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
        if ceased:
            natures_s = (
                (natures_s + "; " if natures_s else "")
                + f"Ceased PSC {str(item.get('ceased_on'))[:10]} — may still hold shares"
            )
        mtype = (
            "corporate"
            if "corporate" in kind or item.get("identification")
            else "individual"
        )
        cn_member = None
        if isinstance(item.get("identification"), dict):
            cn_member = item["identification"].get("registration_number")
        rec = people_map.setdefault(key, _blank_rec(name, member_type=mtype, company_number=cn_member))
        if not ceased:
            rec["is_psc"] = True
            rec["ceased_psc"] = False
        else:
            rec["ceased_psc"] = True
            rec["ceased_on"] = str(item.get("ceased_on") or "")[:10]
        if natures_s:
            rec["natures"].append(natures_s)
        if mtype == "corporate":
            rec["member_type"] = "corporate"
        if cn_member:
            rec["company_number"] = cn_member
        if name and len(name) > len(rec["name"] or ""):
            rec["name"] = name

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
        is_ex = bool(rec.get("resigned_director") or rec.get("ceased_psc")) and not (
            rec.get("is_director") or rec.get("is_psc")
        )
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
            # Enrich notes for exes so Delete is obvious
            if is_ex and (existing.source or "").startswith("ch_"):
                note = (existing.notes or "")
                if "may still hold" not in note.lower() and "former" not in note.lower():
                    existing.notes = (
                        (note + " " if note else "")
                        + "Former officer/PSC — delete if not a shareholder."
                    ).strip()
                    changed = True
            # Cross-ref to people table
            if not existing.person_id:
                link_holding_to_person(db, client, existing, commit=False)
                changed = True
            if changed:
                existing.updated_at = datetime.utcnow()
                stats["holdings_updated"] += 1
            continue

        # Source tag for badges / filtering
        if rec.get("is_director") and rec.get("is_psc"):
            src = "ch_both"
        elif rec.get("is_director"):
            src = "ch_director"
        elif rec.get("is_psc"):
            src = "ch_psc"
        elif rec.get("resigned_director") and rec.get("ceased_psc"):
            src = "ch_ex_both"
        elif rec.get("resigned_director"):
            src = "ch_director_resigned"
        elif rec.get("ceased_psc"):
            src = "ch_psc_ceased"
        else:
            src = "ch_other"

        if is_ex:
            note_bits = [
                "Former officer/PSC on Companies House — included in case they still hold shares "
                "(retiring / silent partners).",
                "Delete if not a member. Set exact shares when known.",
            ]
        else:
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
            ensure_person=True,
        )
        stats["holdings_added"] += 1

    # Final pass: every holding linked to people (manual + CH)
    sync_stats = sync_holdings_to_people(db, client, commit=False)
    stats["people_linked"] = sync_stats.get("linked", 0)

    client.ch_register_seeded_at = datetime.utcnow()
    cap_note = (
        f"issued capital {agg_best:g}" if agg_best is not None else "issued capital unknown — set manually"
    )
    ex_bits = []
    if stats.get("directors_resigned"):
        ex_bits.append(f"{stats['directors_resigned']} ex-director")
    if stats.get("pscs_ceased"):
        ex_bits.append(f"{stats['pscs_ceased']} ceased PSC")
    client.share_register_notes = (
        f"Last CH seed {client.ch_register_seeded_at.date().isoformat()}. "
        f"Directors: {stats['directors']}; PSCs: {stats['pscs']}"
        + (f" (+{', '.join(ex_bits)})" if ex_bits else "")
        + f"; {cap_note}; capital filing hits: {stats['capital_hints']}. "
        "Includes former officers/PSCs — delete anyone who is not a shareholder."
    )
    client.updated_at = datetime.utcnow()
    db.commit()

    msg = (
        f"CH seed: {stats['holdings_added']} potential shareholder(s) "
        f"({stats['directors']} directors"
        + (f", {stats['directors_resigned']} resigned" if stats.get("directors_resigned") else "")
        + f", {stats['pscs']} PSCs"
        + (f", {stats['pscs_ceased']} ceased PSC" if stats.get("pscs_ceased") else "")
        + " — de-duplicated)"
        + (f", {stats['holdings_updated']} updated" if stats["holdings_updated"] else "")
        + (f", cleared {stats['holdings_cleared']} old draft rows" if stats["holdings_cleared"] else "")
        + (f", issued capital {agg_best:g}" if agg_best is not None else ", issued capital not found on CH filings — set Aggregate issued manually")
        + ". Ex-officers included — delete non-shareholders."
    )
    return True, msg, stats


def seed_all_clients_from_ch(
    db: Session,
    *,
    force: bool = False,
    limit: int = 400,
) -> Dict[str, Any]:
    """Seed share registers for active CH-registered companies only."""
    q = (
        db.query(Client)
        .filter(Client.overall_status == "Active")
        .order_by(Client.company_name)
    )
    ok_n = skip_n = err_n = 0
    errors: List[str] = []
    for client in q.limit(limit).all():
        if not client_is_ch_entity(client):
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

    Merges CRM people with CH-seeded shareholdings (by person_id then name key).
    Prefer display names from CRM people when matched.
    """
    from app.services.cs_automation import find_people_matching_officer

    holdings = list_holdings(db, client_id)
    people_list = list(people or [])
    people_by_id = {p.id: p for p in people_list if getattr(p, "id", None)}

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
                "shares": None,
                "is_director": False,
                "is_shareholder": False,
                "is_psc": False,
                "is_other": False,
            }
        return by_key[key]

    def merge_into(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        if source.get("holding") and not target.get("holding"):
            target["holding"] = source["holding"]
        if source.get("person") and not target.get("person"):
            target["person"] = source["person"]
        if source.get("email"):
            target["email"] = source["email"]
        if source.get("phone"):
            target["phone"] = source["phone"]
        target["is_director"] = target["is_director"] or source.get("is_director")
        target["is_psc"] = target["is_psc"] or source.get("is_psc")
        target["is_shareholder"] = target["is_shareholder"] or source.get(
            "is_shareholder"
        )
        if source.get("shares") is not None:
            target["shares"] = source["shares"]
        if source.get("person"):
            target["name"] = (
                source["person"].display_name()
                if hasattr(source["person"], "display_name")
                else source.get("name") or target["name"]
            )

    for h in holdings:
        person = None
        if h.person_id and h.person_id in people_by_id:
            person = people_by_id[h.person_id]
        elif h.person_id:
            person = db.query(Person).filter(Person.id == h.person_id).first()
        if not person:
            # Fuzzy match to client people
            hits = find_people_matching_officer(people_list, h.member_name or "")
            if hits:
                person = _prefer_richest_person(hits)
                h.person_id = person.id

        key = (
            f"person-{person.id}"
            if person
            else (_norm_person_key(h.member_name or "") or f"holding-{h.id}")
        )
        display = (
            person.display_name()
            if person and hasattr(person, "display_name")
            else (h.member_name or "")
        )
        row = ensure(key, display)
        row["holding"] = h
        if person:
            row["person"] = person
            row["name"] = display
            row["email"] = person.email or row.get("email")
            row["phone"] = person.phone or row.get("phone")
        row["is_director"] = row["is_director"] or bool(getattr(h, "is_director", False))
        row["is_psc"] = row["is_psc"] or bool(getattr(h, "is_psc", False))
        if is_shareholder_row(h):
            row["is_shareholder"] = True
            row["shares"] = h.shares

    for p in people_list:
        name = p.display_name() if hasattr(p, "display_name") else (p.full_name or "")
        key = f"person-{p.id}"
        # If a holding already merged under a name key, fold into person key
        name_key = _norm_person_key(name)
        row = ensure(key, name)
        if name_key and name_key in by_key and name_key != key:
            merge_into(row, by_key[name_key])
            del by_key[name_key]
        row["person"] = p
        row["name"] = name
        row["email"] = p.email or row.get("email")
        row["phone"] = p.phone or row.get("phone")
        role = (getattr(p, "role", None) or "").lower()
        if "director" in role:
            row["is_director"] = True
        if "shareholder" in role or "member" in role:
            row["is_shareholder"] = True
        if "psc" in role or "significant control" in role:
            row["is_psc"] = True
        if row.get("holding") and not row["holding"].person_id:
            row["holding"].person_id = p.id

    # Collapse any remaining name-key rows that fuzzy-match a person row
    name_only = [k for k in list(by_key.keys()) if not str(k).startswith("person-")]
    for nk in name_only:
        nrow = by_key[nk]
        if nrow.get("person"):
            continue
        hits = find_people_matching_officer(
            people_list, nrow.get("name") or ""
        )
        if not hits and nrow.get("holding"):
            hits = find_people_matching_officer(
                people_list, nrow["holding"].member_name or ""
            )
        if hits:
            p = _prefer_richest_person(hits)
            pk = f"person-{p.id}"
            prow = ensure(pk, p.display_name() if hasattr(p, "display_name") else p.full_name)
            merge_into(prow, nrow)
            prow["person"] = p
            if nrow.get("holding") and not nrow["holding"].person_id:
                nrow["holding"].person_id = p.id
            del by_key[nk]

    rows = []
    for row in by_key.values():
        if not (row["is_director"] or row["is_shareholder"] or row["is_psc"]):
            row["is_other"] = True
        rows.append(row)
    rows.sort(
        key=lambda r: (
            0 if r["is_director"] else 1,
            0 if r["is_shareholder"] else 1,
            0 if r["is_psc"] else 1,
            (r["name"] or "").lower(),
        )
    )
    return rows


def register_summary(db: Session, client_id: int) -> Dict[str, Any]:
    classes = list_share_classes(db, client_id)
    holdings = list_holdings(db, client_id)
    total_shares = sum(float(h.shares or 0) for h in holdings)
    # Capital pool per class
    pools = []
    for sc in classes:
        issued = float(sc.aggregate_shares) if sc.aggregate_shares is not None else None
        allocated = sum(
            float(h.shares or 0)
            for h in holdings
            if h.share_class_id == sc.id and h.shares is not None
        )
        remaining = (issued - allocated) if issued is not None else None
        pools.append(
            {
                "class": sc,
                "issued": issued,
                "allocated": allocated,
                "remaining": remaining,
            }
        )
    # Default ordinary pool for UI
    total_issued = None
    total_allocated = total_shares
    total_remaining = None
    if pools:
        # Prefer Ordinary
        ord_pool = next(
            (
                p
                for p in pools
                if (p["class"].name or "").lower().startswith("ord")
            ),
            pools[0],
        )
        total_issued = ord_pool["issued"]
        total_allocated = ord_pool["allocated"]
        total_remaining = ord_pool["remaining"]
    return {
        "class_count": len(classes),
        "holding_count": len(holdings),
        "total_shares_known": total_shares,
        "draft_count": sum(1 for h in holdings if (h.status or "") == "draft"),
        "verified_count": sum(1 for h in holdings if (h.status or "") == "verified"),
        "pools": pools,
        "issued": total_issued,
        "allocated": total_allocated,
        "remaining": total_remaining,
    }


def client_is_ch_entity(client: Client) -> bool:
    """True if this client should use Companies House (Ltd / LLP / PLC with real CN)."""
    ct = (client.client_type or "").strip().lower()
    if ct in (
        "sole trader",
        "partnership",
        "individual",
        "business",  # often unincorporated
    ):
        return False
    cn = normalize_company_number(client.company_number or "") or ""
    cu = cn.upper()
    if not cu or cu.startswith("IND-") or cu.startswith("PENDING"):
        return False
    # Partnership postcodes / junk numbers
    if ct == "partnership":
        return False
    # Valid CH company numbers: 8 digits or SC/NI/OC/SO/etc prefixes
    if cu.isdigit() and len(cu) == 8:
        return True
    if len(cu) >= 2 and cu[:2].isalpha() and cu[2:].isdigit():
        return True
    # Default: if type is Limited / LLP / PLC, allow when CN looks real
    if ct in ("limited company", "llp", "plc", "limited"):
        return len(cu) >= 6
    # Type blank but real CN → treat as CH entity
    if (not ct or ct == "other") and (
        (cu.isdigit() and len(cu) == 8)
        or (len(cu) >= 2 and cu[:2].isalpha() and any(ch.isdigit() for ch in cu))
    ):
        return True
    return False
