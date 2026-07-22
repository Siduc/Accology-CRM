"""Confirmation Statement automation: download, pre-fill, review workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models import Client, Job
from app.models.cs_pack import CsPack
from app.services.companies_house import download_cs_bundle
from app.services.company_numbers import normalize_company_number
from app.services.dates import calculate_dates


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _addr_block(addr: Optional[Dict[str, Any]]) -> str:
    if not addr:
        return ""
    parts = [
        addr.get("address_line_1"),
        addr.get("address_line_2"),
        addr.get("locality"),
        addr.get("region"),
        addr.get("postal_code"),
        addr.get("country"),
    ]
    return ", ".join(p for p in parts if p)


def build_form_from_download(
    bundle: Dict[str, Any], client: Client
) -> Dict[str, Any]:
    """Normalize CH bundle into review form sections."""
    profile = bundle.get("profile") or {}
    officers_raw = (bundle.get("officers") or {}).get("items") or []
    pscs_raw = (bundle.get("pscs") or {}).get("items") or []
    cs = profile.get("confirmation_statement") or {}
    ro = profile.get("registered_office_address") or {}
    sic = profile.get("sic_codes") or []

    officers = []
    for o in officers_raw:
        if o.get("resigned_on"):
            continue
        officers.append(
            {
                "name": o.get("name") or "—",
                "role": (o.get("officer_role") or "").replace("-", " ").title(),
                "appointed_on": o.get("appointed_on"),
                "nationality": o.get("nationality"),
                "country_of_residence": o.get("country_of_residence"),
            }
        )

    pscs = []
    for p in pscs_raw:
        if (p.get("ceased_on") or p.get("kind") or "").endswith("statement"):
            # keep statements too as info
            pass
        name = p.get("name")
        if not name and p.get("name_elements"):
            ne = p["name_elements"]
            name = " ".join(
                x
                for x in [
                    ne.get("title"),
                    ne.get("forename"),
                    ne.get("middle_name"),
                    ne.get("surname"),
                ]
                if x
            )
        pscs.append(
            {
                "name": name or p.get("kind") or "—",
                "kind": p.get("kind"),
                "natures_of_control": p.get("natures_of_control") or [],
                "notified_on": p.get("notified_on"),
                "ceased_on": p.get("ceased_on"),
            }
        )

    made_up = _parse_date(cs.get("next_made_up_to") or cs.get("last_made_up_to"))
    due = _parse_date(cs.get("next_due"))
    if made_up and not due:
        due = made_up  # fallback; profile often has both

    return {
        "company_name": profile.get("company_name") or client.company_name,
        "company_number": profile.get("company_number")
        or normalize_company_number(client.company_number or ""),
        "company_status": profile.get("company_status"),
        "company_type": profile.get("type"),
        "registered_office": _addr_block(ro),
        "registered_office_raw": ro,
        "sic_codes": sic,
        "officers": officers,
        "pscs": pscs,
        "cs_made_up_to": made_up.isoformat() if made_up else None,
        "cs_due": due.isoformat() if due else None,
        "cs_overdue": bool(cs.get("overdue")),
        "cs_last_made_up_to": cs.get("last_made_up_to"),
        "auth_code_on_file": bool((client.ch_authentication_code or "").strip()),
        "confirmed_accurate": False,
        "changes_needed": False,
        "checklist_notes": "",
    }


def webfiling_url(company_number: str) -> str:
    """Handoff to official CH confirmation statement filing guidance."""
    cn = normalize_company_number(company_number) or ""
    # Stable GOV.UK entry; WebFiling requires login/auth code in-browser
    return (
        "https://www.gov.uk/file-your-confirmation-statement-with-companies-house"
        + (f"?company-number={quote(cn)}" if cn else "")
    )


def company_public_url(company_number: str) -> str:
    cn = normalize_company_number(company_number) or ""
    if not cn:
        return "https://find-and-update.company-information.service.gov.uk/"
    return f"https://find-and-update.company-information.service.gov.uk/company/{cn}"


@dataclass
class PackResult:
    ok: bool
    pack: Optional[CsPack] = None
    error: str = ""


def create_or_refresh_pack(
    db: Session,
    client_id: int,
    *,
    job_id: Optional[int] = None,
    prepared_by: Optional[str] = None,
    force_new: bool = False,
) -> PackResult:
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return PackResult(ok=False, error="Client not found.")
    cn = normalize_company_number(client.company_number or "")
    if not cn or cn.upper().startswith("IND-") or cn.upper().startswith("PENDING"):
        return PackResult(
            ok=False,
            error="Client needs a valid Companies House company number.",
        )

    fetched = download_cs_bundle(cn)
    if not fetched.ok:
        return PackResult(ok=False, error=fetched.error or "CH download failed.")

    bundle = fetched.profile
    form = build_form_from_download(bundle, client)
    made_up = _parse_date(form.get("cs_made_up_to"))
    due = _parse_date(form.get("cs_due"))

    pack: Optional[CsPack] = None
    if not force_new:
        pack = (
            db.query(CsPack)
            .filter(CsPack.client_id == client_id)
            .filter(CsPack.status.in_(["draft", "in_review", "ready_to_file"]))
            .order_by(CsPack.id.desc())
            .first()
        )

    if not pack:
        pack = CsPack(client_id=client_id, status="draft")
        db.add(pack)

    pack.company_number = cn
    pack.made_up_to = made_up
    pack.due_on = due
    pack.ch_snapshot_json = json.dumps(bundle, default=str)
    pack.form_json = json.dumps(form, default=str)
    pack.status = "in_review"
    pack.prepared_by = prepared_by
    pack.updated_at = datetime.utcnow()
    if job_id:
        pack.job_id = job_id
    elif not pack.job_id:
        pack.job_id = _find_or_create_cs_job(db, client, made_up, due)

    # Keep client name in sync with CH when available
    ch_name = form.get("company_name")
    if ch_name and ch_name != client.company_name:
        client.company_name = ch_name

    db.commit()
    db.refresh(pack)
    return PackResult(ok=True, pack=pack)


def _find_or_create_cs_job(
    db: Session,
    client: Client,
    made_up: Optional[date],
    due: Optional[date],
) -> Optional[int]:
    q = (
        db.query(Job)
        .filter(Job.client_id == client.id)
        .filter(Job.type == "Confirmation Statement")
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
    )
    if made_up:
        job = q.filter(Job.period_end == made_up).first()
        if job:
            return job.id
    job = q.order_by(Job.id.desc()).first()
    if job:
        return job.id

    # Create a planned CS job from dates
    pe = made_up
    statutory = due
    ts = tc = None
    if pe:
        try:
            statutory, ts, tc = calculate_dates("Confirmation Statement", pe)
        except Exception:
            statutory = due or pe
    if due:
        statutory = due
    job = Job(
        title=f"Confirmation Statement — {client.display_name()} — {pe or 'pending'}",
        type="Confirmation Statement",
        client_id=client.id,
        period_end=pe,
        statutory_due_date=statutory or due,
        target_start=ts,
        target_completion=tc or due,
        status="Planned",
        is_recurring="Yes",
        source="companies_house",
        notes="Created with CS review pack from Companies House data.",
    )
    db.add(job)
    db.flush()
    return job.id


def get_pack(db: Session, pack_id: int) -> Optional[CsPack]:
    return db.query(CsPack).filter(CsPack.id == pack_id).first()


def latest_pack_for_client(db: Session, client_id: int) -> Optional[CsPack]:
    return (
        db.query(CsPack)
        .filter(CsPack.client_id == client_id)
        .order_by(CsPack.id.desc())
        .first()
    )


def form_dict(pack: CsPack) -> Dict[str, Any]:
    if not pack.form_json:
        return {}
    try:
        return json.loads(pack.form_json)
    except json.JSONDecodeError:
        return {}


def _norm_name(name: str) -> str:
    """Normalise person names for fuzzy CH ↔ CRM matching."""
    s = (name or "").upper()
    for ch in (",", ".", "'", '"', "-"):
        s = s.replace(ch, " ")
    # Drop common titles
    for t in (
        "MR ",
        "MRS ",
        "MISS ",
        "MS ",
        "DR ",
        "SIR ",
        "LADY ",
        "LORD ",
    ):
        if s.startswith(t):
            s = s[len(t) :]
    parts = [p for p in s.split() if p]
    return " ".join(sorted(parts))  # order-independent: "DAVIES PHILIP" == "PHILIP DAVIES"


def _names_match(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Containment of all surname tokens (last token often surname on CH "SURNAME, Forename")
    sa, sb = set(na.split()), set(nb.split())
    if len(sa) >= 2 and len(sb) >= 2 and sa == sb:
        return True
    # Partial: all of the shorter set in the longer
    if sa <= sb or sb <= sa:
        return True
    return False


def _fmt_d(value: Any) -> str:
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d-%m-%Y")
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return str(value)


def _norm_address(text: str) -> str:
    """Normalise address text for CH ↔ CRM compare (CRM has no country field)."""
    s = (text or "").upper()
    for noise in (
        "UNITED KINGDOM",
        "GREAT BRITAIN",
        "ENGLAND",
        "SCOTLAND",
        "WALES",
        "NORTHERN IRELAND",
        "UK",
        "U.K.",
    ):
        s = s.replace(noise, " ")
    for ch in (",", ".", ";", "#"):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _addresses_match(ch_text: str, practice_text: str, raw: Optional[Dict[str, Any]] = None) -> bool:
    """
    True if practice address is essentially the same as CH RO.
    Handles missing country on CRM and minor punctuation differences.
    """
    ch_n = _norm_address(ch_text)
    pr_n = _norm_address(practice_text)
    if not ch_n and not pr_n:
        return True
    if not pr_n:
        return False
    if ch_n == pr_n:
        return True
    # CH string often includes country; practice may be a subset
    if pr_n and pr_n in ch_n:
        return True
    # Field-level: line1 + postcode is enough
    raw = raw or {}
    line1 = _norm_address(str(raw.get("address_line_1") or ""))
    postcode = _norm_address(str(raw.get("postal_code") or ""))
    locality = _norm_address(str(raw.get("locality") or ""))
    if postcode and postcode not in pr_n:
        return False
    if line1 and line1 not in pr_n and not any(
        tok in pr_n for tok in line1.split() if len(tok) > 4
    ):
        # require most of line1 tokens
        tokens = [t for t in line1.split() if len(t) > 2]
        if tokens:
            hit = sum(1 for t in tokens if t in pr_n)
            if hit < max(1, len(tokens) // 2):
                return False
    if locality and locality not in pr_n:
        return False
    if postcode and postcode in pr_n:
        return True
    return False


def _ch_accounts_dates(pack: CsPack) -> Dict[str, Any]:
    """Extract accounts dates from stored CH snapshot (if present)."""
    out: Dict[str, Any] = {
        "period_end": None,
        "due": None,
        "last_made_up_to": None,
        "overdue": False,
        "ard": "",
    }
    if not pack.ch_snapshot_json:
        return out
    try:
        snap = json.loads(pack.ch_snapshot_json)
    except json.JSONDecodeError:
        return out
    profile = snap.get("profile") or {}
    accounts = profile.get("accounts") or {}
    next_acc = accounts.get("next_accounts") or {}
    last_acc = accounts.get("last_accounts") or {}
    ard = accounts.get("accounting_reference_date") or {}
    out["period_end"] = _parse_date(
        next_acc.get("period_end_on") or accounts.get("next_made_up_to")
    )
    out["due"] = _parse_date(next_acc.get("due_on") or accounts.get("next_due"))
    out["last_made_up_to"] = _parse_date(
        last_acc.get("made_up_to") or last_acc.get("period_end_on")
    )
    out["overdue"] = bool(next_acc.get("overdue") or accounts.get("overdue"))
    if ard.get("day") and ard.get("month"):
        out["ard"] = f"{int(ard['day']):02d}/{int(ard['month']):02d}"
    return out


def apply_ch_address_to_client(client: Client, pack: CsPack) -> bool:
    """Copy CH registered office into CRM address fields. Returns True if applied."""
    form = form_dict(pack)
    raw = form.get("registered_office_raw") or {}
    if not raw and pack.ch_snapshot_json:
        try:
            snap = json.loads(pack.ch_snapshot_json)
            raw = (snap.get("profile") or {}).get("registered_office_address") or {}
        except json.JSONDecodeError:
            raw = {}
    if not raw and form.get("registered_office"):
        # Fallback: put full CH block on line1 if structured address missing
        client.address_line1 = str(form.get("registered_office"))
        client.updated_at = datetime.utcnow()
        return True
    if not raw:
        return False
    # Always overwrite from CH when user explicitly requests populate
    client.address_line1 = raw.get("address_line_1") or None
    client.address_line2 = raw.get("address_line_2") or None
    client.town = raw.get("locality") or None
    client.postcode = raw.get("postal_code") or None
    client.updated_at = datetime.utcnow()
    return True


def create_contact_from_officer(
    db: Session,
    client: Client,
    *,
    officer_name: str,
    officer_role: str = "",
) -> Optional[Any]:
    """Create a Person linked to client from a CH officer name."""
    from app.models.person import Person

    name = (officer_name or "").strip()
    if not name:
        return None
    # CH format often "SURNAME, Forename Middle"
    display = name
    if "," in name:
        sur, rest = name.split(",", 1)
        display = f"{rest.strip()} {sur.strip()}".strip()
    person = Person(
        full_name=display,
        role=(officer_role or "Director").strip() or "Director",
        person_status="Contact",
        is_primary=False,
        notes=f"Created from CS compare (CH officer: {name})",
    )
    person.clients.append(client)
    db.add(person)
    db.flush()
    return person


def unlink_person_from_client(
    db: Session, client: Client, person_id: int
) -> bool:
    from app.models.person import Person

    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        return False
    if client in person.clients:
        person.clients.remove(client)
        if person.is_primary:
            person.is_primary = False
        db.flush()
        return True
    return False


def sync_accounts_job_from_ch(
    db: Session, client: Client, pack: CsPack
) -> Optional[Job]:
    """
    Align open Accounts job period/due with CH next accounts dates.
    Creates a Planned job if none open. Returns the job updated/created.
    """
    ch = _ch_accounts_dates(pack)
    pe = ch.get("period_end")
    due = ch.get("due")
    if not pe:
        return None
    job = (
        db.query(Job)
        .filter(Job.client_id == client.id)
        .filter(Job.type == "Accounts")
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
        .order_by(Job.id.desc())
        .first()
    )
    statutory = due
    ts = tc = None
    try:
        statutory, ts, tc = calculate_dates("Accounts", pe)
    except Exception:
        pass
    if due:
        statutory = due
    if not job:
        job = Job(
            title=f"Accounts — {client.display_name()} — {pe}",
            type="Accounts",
            client_id=client.id,
            period_end=pe,
            statutory_due_date=statutory,
            target_start=ts,
            target_completion=tc or statutory,
            status="Planned",
            is_recurring="Yes",
            source="companies_house",
            notes="Period/due set from CS pack CH compare.",
        )
        db.add(job)
    else:
        job.period_end = pe
        job.statutory_due_date = statutory
        if ts:
            job.target_start = ts
        if tc or statutory:
            job.target_completion = tc or statutory
        job.title = f"Accounts — {client.display_name()} — {pe}"
        job.updated_at = datetime.utcnow()
        note = (job.notes or "").strip()
        stamp = f"Accounts dates synced from CH via CS compare ({datetime.utcnow().date().isoformat()})."
        job.notes = f"{note}\n{stamp}".strip() if note else stamp
    db.flush()
    return job


def fix_cs_job_title(db: Session, client: Client, pack: CsPack, job: Optional[Job]) -> bool:
    """Rewrite linked CS job title from CH company name + made-up-to."""
    if not job:
        return False
    form = form_dict(pack)
    name = form.get("company_name") or client.display_name()
    pe = pack.made_up_to or _parse_date(form.get("cs_made_up_to")) or job.period_end
    job.title = f"Confirmation Statement — {name} — {pe or 'pending'}"
    if pe:
        job.period_end = pe
    due = pack.due_on or _parse_date(form.get("cs_due"))
    if due:
        job.statutory_due_date = due
    job.updated_at = datetime.utcnow()
    return True


def build_cs_comparison(
    pack: CsPack,
    client: Optional[Client],
    *,
    people: Optional[List[Any]] = None,
    job: Optional[Job] = None,
    accounts_job: Optional[Job] = None,
) -> Dict[str, Any]:
    """
    Compare Companies House snapshot (pack form) with Accologise client record.

    Flags mismatches (name, year/period, officers vs contacts) and practice-only
    fields that do not come from the public CH API (auth codes, gateway, software).
    """
    form = form_dict(pack)
    people = list(people or [])
    ch_name = (form.get("company_name") or "").strip()
    crm_name = (client.company_name or "").strip() if client else ""
    ch_number = (
        form.get("company_number") or pack.company_number or ""
    ).strip()
    crm_number = (client.company_number or "").strip() if client else ""

    # --- Company identity rows ---
    def row(
        field: str,
        ch_val: str,
        practice_val: str,
        *,
        source_note: str = "",
        match: Optional[bool] = None,
        severity: str = "info",
        fix_action: str = "",
        job_id: Optional[int] = None,
        job_label: str = "",
    ) -> Dict[str, Any]:
        if match is None:
            ch_n = (ch_val or "").strip().upper()
            pr_n = (practice_val or "").strip().upper()
            if not ch_n and not pr_n:
                match = True
            elif not ch_n or not pr_n:
                match = False
            else:
                match = ch_n == pr_n
        if match:
            severity = "ok"
        elif severity == "info":
            severity = "warn"
        return {
            "field": field,
            "ch": ch_val or "—",
            "practice": practice_val or "—",
            "match": bool(match),
            "severity": severity,
            "note": source_note,
            "fix_action": fix_action or "",
            "job_id": job_id,
            "job_label": job_label or "",
        }

    company_rows = [
        row(
            "Company name",
            ch_name,
            crm_name,
            source_note="CH public profile vs client record",
            severity="error" if ch_name and crm_name and ch_name.upper() != crm_name.upper() else "info",
        ),
        row(
            "Company number",
            ch_number,
            crm_number,
            source_note="Must match for filing",
            severity="error",
        ),
        row(
            "Company status",
            str(form.get("company_status") or ""),
            (client.overall_status or "") if client else "",
            source_note="CH status vs practice book status (not always comparable)",
            match=True,  # informational only
        ),
        row(
            "Registered office",
            str(form.get("registered_office") or ""),
            (client.address_block() if client else "") or "",
            source_note="CH RO vs CRM address (country ignored — not stored on client)",
            match=_addresses_match(
                str(form.get("registered_office") or ""),
                (client.address_block() if client else "") or "",
                form.get("registered_office_raw")
                if isinstance(form.get("registered_office_raw"), dict)
                else None,
            ),
            fix_action="address"
            if client
            and not _addresses_match(
                str(form.get("registered_office") or ""),
                (client.address_block() if client else "") or "",
                form.get("registered_office_raw")
                if isinstance(form.get("registered_office_raw"), dict)
                else None,
            )
            else "",
        ),
        row(
            "SIC codes",
            ", ".join(form.get("sic_codes") or []) or "",
            "—",
            source_note="SIC only held at CH in Accologise today",
            match=True,
        ),
    ]

    # Period / year
    ch_made = pack.made_up_to or _parse_date(form.get("cs_made_up_to"))
    ch_due = pack.due_on or _parse_date(form.get("cs_due"))
    job_pe = job.period_end if job else None
    job_due = job.statutory_due_date if job else None
    pe_match = True
    if ch_made and job_pe:
        pe_match = ch_made == job_pe
    company_rows.append(
        row(
            "CS made up to (period)",
            _fmt_d(ch_made),
            _fmt_d(job_pe) if job else "— no CS job linked —",
            source_note="CH confirmation_statement.next_made_up_to vs job period end",
            match=pe_match if job and ch_made else None,
            severity="error" if job and ch_made and not pe_match else "info",
            fix_action="cs-job-title" if job and ch_made and not pe_match else "",
            job_id=job.id if job else None,
            job_label="Open CS job" if job else "",
        )
    )
    company_rows.append(
        row(
            "CS due date",
            _fmt_d(ch_due),
            _fmt_d(job_due) if job else "—",
            source_note="CH next_due vs job statutory due",
            match=(ch_due == job_due) if (ch_due and job_due) else None,
            fix_action="cs-job-title"
            if job and ch_due and job_due and ch_due != job_due
            else "",
            job_id=job.id if job else None,
            job_label="Open CS job" if job else "",
        )
    )

    # Accounts dates (often wrong in CRM if ARD/year end was never refreshed from CH)
    ch_acc = _ch_accounts_dates(pack)
    acc_pe = accounts_job.period_end if accounts_job else None
    acc_due = accounts_job.statutory_due_date if accounts_job else None
    acc_pe_match = (
        (ch_acc.get("period_end") == acc_pe)
        if (ch_acc.get("period_end") and acc_pe)
        else None
    )
    acc_due_match = (
        (ch_acc.get("due") == acc_due) if (ch_acc.get("due") and acc_due) else None
    )
    company_rows.append(
        row(
            "Accounts period end (next)",
            _fmt_d(ch_acc.get("period_end")),
            _fmt_d(acc_pe) if accounts_job else "— no open Accounts job —",
            source_note=(
                f"CH next accounts period_end"
                + (f" · ARD {ch_acc['ard']}" if ch_acc.get("ard") else "")
                + " vs open Accounts job period_end"
            ),
            match=acc_pe_match if accounts_job and ch_acc.get("period_end") else None,
            severity="error"
            if accounts_job and ch_acc.get("period_end") and not acc_pe_match
            else "info",
            fix_action="accounts-dates" if ch_acc.get("period_end") else "",
            job_id=accounts_job.id if accounts_job else None,
            job_label="Open Accounts job" if accounts_job else "Create/sync Accounts job",
        )
    )
    company_rows.append(
        row(
            "Accounts due date (next)",
            _fmt_d(ch_acc.get("due"))
            + (" OVERDUE" if ch_acc.get("overdue") else ""),
            _fmt_d(acc_due) if accounts_job else "—",
            source_note="CH next_accounts.due_on vs Accounts job statutory due",
            match=acc_due_match if accounts_job and ch_acc.get("due") else None,
            severity="error"
            if accounts_job and ch_acc.get("due") and not acc_due_match
            else "info",
            fix_action="accounts-dates" if ch_acc.get("due") else "",
            job_id=accounts_job.id if accounts_job else None,
            job_label="Open Accounts job" if accounts_job else "Create/sync Accounts job",
        )
    )
    if ch_acc.get("last_made_up_to"):
        company_rows.append(
            row(
                "Accounts last filed (CH)",
                _fmt_d(ch_acc.get("last_made_up_to")),
                "— (history on CH)",
                source_note="CH last_accounts.made_up_to — reference only",
                match=True,
            )
        )

    if job and job.title:
        # Flag wrong company name embedded in job title (e.g. leftover "Test Co")
        title_ok = True
        if ch_name:
            # any significant word from CH name in title, or display name
            words = [
                w
                for w in ch_name.upper()
                .replace("LIMITED", "")
                .replace("LTD", "")
                .split()
                if len(w) > 3
            ]
            title_u = (job.title or "").upper()
            title_ok = any(w in title_u for w in words) if words else True
        company_rows.append(
            row(
                "Linked CS job title",
                f"Confirmation Statement — {ch_name or '…'} — {ch_made or 'pending'}",
                job.title,
                source_note="Job title should name this company — wrong name/year often means stale job",
                match=title_ok,
                severity="error" if not title_ok else "info",
                fix_action="cs-job-title" if not title_ok else "",
                job_id=job.id,
                job_label="Open CS job",
            )
        )

    # --- Officers vs people ---
    officers = form.get("officers") or []
    officer_matches = []
    matched_person_ids = set()
    for o in officers:
        oname = o.get("name") or ""
        hit = None
        for p in people:
            if p.id in matched_person_ids:
                continue
            if _names_match(oname, p.full_name or ""):
                hit = p
                matched_person_ids.add(p.id)
                break
        officer_matches.append(
            {
                "ch_name": oname,
                "ch_role": o.get("role") or "",
                "ch_appointed": o.get("appointed_on") or "",
                "practice_name": hit.full_name if hit else "— not on CRM contacts —",
                "practice_role": (hit.role or "") if hit else "",
                "practice_id": hit.id if hit else None,
                "match": bool(hit),
                "severity": "ok" if hit else "warn",
            }
        )

    crm_only_people = []
    for p in people:
        if p.id in matched_person_ids:
            continue
        # Skip practice staff if tagged? Keep all as "CRM only"
        crm_only_people.append(
            {
                "name": p.full_name or "—",
                "role": p.role or "",
                "email": p.email or "",
                "id": p.id,
                "is_primary": bool(p.is_primary),
                "severity": "warn",
                "note": "On Accologise contacts but not an active CH officer in this download",
            }
        )

    # --- Practice-only secrets / codes (not from public CH API) ---
    practice_only = []
    if client:
        def _secret_row(label: str, present: bool, detail: str = "") -> Dict[str, Any]:
            return {
                "field": label,
                "present": present,
                "detail": detail or ("On file" if present else "Not set"),
                "note": "Practice / client-held — not returned by Companies House public API",
                "severity": "info",
            }

        practice_only = [
            _secret_row(
                "CH authentication code",
                bool((client.ch_authentication_code or "").strip()),
                "Required for WebFiling — never shown on public CH record",
            ),
            _secret_row(
                "CH personal code",
                bool((client.ch_personal_code or "").strip()),
            ),
            _secret_row(
                "Government Gateway",
                bool((client.gov_gateway_username or "").strip()),
            ),
            _secret_row(
                "Accounts software login",
                bool(
                    (client.accounts_software_id or "").strip()
                    or (client.xero_username or "").strip()
                ),
            ),
            _secret_row(
                "CRM contact email/phone on client",
                bool((client.email or "").strip() or (client.phone or "").strip()),
            ),
        ]

    mismatches = [r for r in company_rows if not r.get("match")]
    officer_gaps = [o for o in officer_matches if not o.get("match")]
    accounts_mismatch = bool(
        accounts_job
        and ch_acc.get("period_end")
        and acc_pe
        and ch_acc.get("period_end") != acc_pe
    )
    summary = {
        "mismatch_count": len(mismatches),
        "officer_unmatched": len(officer_gaps),
        "crm_only_contacts": len(crm_only_people),
        "practice_secrets_on_file": sum(
            1 for s in practice_only if s.get("present")
        ),
        "accounts_mismatch": accounts_mismatch,
        "ok": len(mismatches) == 0 and len(officer_gaps) == 0,
    }

    fetched_at = None
    if pack.ch_snapshot_json:
        try:
            snap = json.loads(pack.ch_snapshot_json)
            fetched_at = snap.get("fetched_at")
        except json.JSONDecodeError:
            fetched_at = None

    can_fix = {
        "address": bool(
            form.get("registered_office_raw")
            or (
                pack.ch_snapshot_json
                and "registered_office" in (pack.ch_snapshot_json or "")
            )
        ),
        "accounts_dates": bool(ch_acc.get("period_end")),
        "cs_job_title": bool(job),
    }

    return {
        "company_rows": company_rows,
        "officers": officer_matches,
        "crm_only_people": crm_only_people,
        "practice_only": practice_only,
        "summary": summary,
        "ch_fetched_at": fetched_at,
        "ch_accounts": ch_acc,
        "can_fix": can_fix,
        "accounts_job_id": accounts_job.id if accounts_job else None,
    }


def save_review(
    db: Session,
    pack_id: int,
    *,
    review_notes: str = "",
    confirmed_no_changes: str = "",
    checklist_notes: str = "",
    confirmed_accurate: bool = False,
    changes_needed: bool = False,
) -> PackResult:
    pack = get_pack(db, pack_id)
    if not pack:
        return PackResult(ok=False, error="Pack not found.")
    form = form_dict(pack)
    form["confirmed_accurate"] = confirmed_accurate
    form["changes_needed"] = changes_needed
    form["checklist_notes"] = checklist_notes
    pack.form_json = json.dumps(form, default=str)
    pack.review_notes = review_notes or None
    pack.confirmed_no_changes = (confirmed_no_changes or "").strip() or None
    if pack.status == "draft":
        pack.status = "in_review"
    pack.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pack)
    return PackResult(ok=True, pack=pack)


def mark_ready(db: Session, pack_id: int) -> PackResult:
    pack = get_pack(db, pack_id)
    if not pack:
        return PackResult(ok=False, error="Pack not found.")
    pack.status = "ready_to_file"
    pack.ready_at = datetime.utcnow()
    pack.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pack)
    return PackResult(ok=True, pack=pack)


def mark_filed(db: Session, pack_id: int, *, complete_job: bool = True) -> PackResult:
    pack = get_pack(db, pack_id)
    if not pack:
        return PackResult(ok=False, error="Pack not found.")
    pack.status = "filed"
    pack.filed_at = datetime.utcnow()
    pack.updated_at = datetime.utcnow()
    if complete_job and pack.job_id:
        job = db.query(Job).filter(Job.id == pack.job_id).first()
        if job and job.status not in ("Completed", "Cancelled"):
            job.status = "Completed"
            if not job.actual_completion:
                job.actual_completion = date.today()
    db.commit()
    db.refresh(pack)
    return PackResult(ok=True, pack=pack)


def export_pack_text(pack: CsPack, client: Optional[Client] = None) -> str:
    """
    Practice file note / checklist for the client record.

    Not a re-key sheet for WebFiling: CH already holds register data;
    this documents what was reviewed in Accologise before you file online.
    """
    form = form_dict(pack)
    lines = [
        "Accologise — Confirmation Statement review pack",
        "=" * 48,
        f"Pack ID: {pack.id}",
        f"Status: {pack.status}",
        f"Client: {(client.display_name() if client else pack.client_id)}",
        f"Company number: {pack.company_number or form.get('company_number') or '—'}",
        f"Made up to: {pack.made_up_to or form.get('cs_made_up_to') or '—'}",
        f"Due: {pack.due_on or form.get('cs_due') or '—'}",
        f"Auth code on file: {'Yes' if form.get('auth_code_on_file') else 'No'}",
        "",
        "Company status: " + str(form.get("company_status") or "—"),
        "Registered office:",
        "  " + str(form.get("registered_office") or "—"),
        "SIC codes: " + (", ".join(form.get("sic_codes") or []) or "—"),
        "",
        "Officers (active):",
    ]
    for o in form.get("officers") or []:
        lines.append(
            f"  - {o.get('name')} · {o.get('role')} · appointed {o.get('appointed_on') or '—'}"
        )
    if not form.get("officers"):
        lines.append("  (none listed)")
    lines.append("")
    lines.append("PSCs:")
    for p in form.get("pscs") or []:
        noc = ", ".join(p.get("natures_of_control") or [])
        lines.append(f"  - {p.get('name')} · {noc or p.get('kind') or '—'}")
    if not form.get("pscs"):
        lines.append("  (none listed)")
    lines.extend(
        [
            "",
            "Review notes:",
            pack.review_notes or form.get("checklist_notes") or "(none)",
            "",
            "Confirmed no changes: " + str(pack.confirmed_no_changes or "—"),
            f"Ready at: {pack.ready_at or '—'}",
            f"Filed at (practice): {pack.filed_at or '—'}",
            "",
            "Filing: complete on Companies House WebFiling (or filing software)",
            "using the company authentication code. This export is a practice",
            "file note, not an electronic submission to Companies House.",
            webfiling_url(pack.company_number or ""),
        ]
    )
    return "\n".join(lines) + "\n"
