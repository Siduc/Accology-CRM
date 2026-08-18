"""Client playbooks, AGENTS.md, and Current / prior-year working-paper folders."""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.client_playbook import (
    BOOKKEEPING_SOURCES,
    SOURCE_CODES,
    ClientPlaybook,
)
from app.models.job import Job
from app.services.ms_graph_drive import sanitize_segment

CURRENT_PACK_SUBFOLDERS = ("Source", "Working Papers", "Journals", "IRIS Import")
STANDING_CATEGORIES = (
    "Engagement Letter",
    "Accounts",
    "Tax Return",
    "ID-KYC",
    "Correspondence",
    "Working Papers",
    "Invoices",
    "Proposals",
    "Other",
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
MOVE_CAP = 80


def practice_files_root() -> Path:
    from app.config import PRACTICE_FILES_ROOT

    return Path(PRACTICE_FILES_ROOT)


def live_clients_query(db: Session):
    return db.query(Client).filter(
        (Client.overall_status.is_(None))
        | (Client.overall_status != "Inactive"),
        Client.disengagement_date.is_(None),
    )


def is_live_client(client: Client) -> bool:
    status = (client.overall_status or "Active").strip()
    if status.lower() == "inactive":
        return False
    if client.disengagement_date:
        return False
    return True


def get_playbook(db: Session, client_id: int) -> Optional[ClientPlaybook]:
    return (
        db.query(ClientPlaybook)
        .filter(ClientPlaybook.client_id == client_id)
        .first()
    )


def get_or_create_playbook(db: Session, client_id: int) -> ClientPlaybook:
    row = get_playbook(db, client_id)
    if row:
        return row
    row = ClientPlaybook(client_id=client_id, bookkeeping_source="xero")
    db.add(row)
    db.flush()
    return row


def source_label(code: Optional[str]) -> str:
    for c, label in BOOKKEEPING_SOURCES:
        if c == (code or ""):
            return label
    return (code or "Xero").strip() or "Xero"


SALES_LEDGER_ONLY_LINE = "Sage is sales ledger only."
SALES_LEDGER_ONLY_NOTE = (
    "Sage Business Cloud is the sales ledger only — invoices, credit notes, "
    "customers and aged debtors. Bank and year-end journals are not in Sage."
)
_SALES_LEDGER_ONLY_MARKERS = (
    "sales ledger only",
    "sales-ledger-only",
    "only uses sage for the sales ledger",
    "sage for the sales ledger",
    "sage for running the sales ledger",
)


def is_sales_ledger_only_text(text: Optional[str]) -> bool:
    blob = (text or "").lower()
    return any(marker in blob for marker in _SALES_LEDGER_ONLY_MARKERS)


def is_sales_ledger_only(playbook: Optional[ClientPlaybook]) -> bool:
    """True when Sage is used for invoices/debtors only, not full books."""
    if not playbook:
        return False
    return is_sales_ledger_only_text(f"{playbook.quirks or ''} {playbook.source_notes or ''}")


def apply_sales_ledger_only_flag(
    source_notes: str, quirks: str, enabled: bool
) -> Tuple[str, str]:
    notes = (source_notes or "").strip()
    q = (quirks or "").strip()
    if enabled:
        if not is_sales_ledger_only_text(notes):
            notes = f"{SALES_LEDGER_ONLY_NOTE} {notes}".strip()
        if not is_sales_ledger_only_text(q):
            q = f"{SALES_LEDGER_ONLY_LINE} {q}".strip()
        return notes, q
    q = re.sub(rf"^{re.escape(SALES_LEDGER_ONLY_LINE)}\s*", "", q).strip()
    notes = re.sub(
        rf"^{re.escape(SALES_LEDGER_ONLY_NOTE)}\s*",
        "",
        notes,
        flags=re.I,
    ).strip()
    return notes, q


def current_accounts_year(db: Session, client: Client, playbook: Optional[ClientPlaybook] = None) -> int:
    if playbook and playbook.current_year:
        try:
            y = int(playbook.current_year)
            if 2000 <= y <= 2100:
                return y
        except (TypeError, ValueError):
            pass
    open_job = (
        db.query(Job)
        .filter(
            Job.client_id == client.id,
            Job.type == "Accounts",
            Job.period_end.isnot(None),
            ~Job.status.in_(["Completed", "Lost", "Cancelled", "Filed"]),
        )
        .order_by(Job.period_end.desc())
        .first()
    )
    if open_job and open_job.period_end:
        return int(open_job.period_end.year)
    last_job = (
        db.query(Job)
        .filter(
            Job.client_id == client.id,
            Job.type == "Accounts",
            Job.period_end.isnot(None),
        )
        .order_by(Job.period_end.desc())
        .first()
    )
    if last_job and last_job.period_end:
        return int(last_job.period_end.year)
    if playbook and playbook.year_end_month:
        today = date.today()
        ye_m = int(playbook.year_end_month)
        ye_d = int(playbook.year_end_day or 31)
        try:
            this_ye = date(today.year, ye_m, min(ye_d, 28 if ye_m == 2 else ye_d))
        except ValueError:
            this_ye = date(today.year, ye_m, 28)
        return today.year if today >= this_ye else today.year - 1
    return date.today().year


def client_folder_name(client: Client) -> str:
    if hasattr(client, "display_name"):
        try:
            name = client.display_name() or ""
        except Exception:
            name = ""
    else:
        name = ""
    name = name or (client.company_name or "").strip() or f"Client {client.id}"
    return sanitize_segment(name)


def client_root_path(client: Client) -> Path:
    return practice_files_root() / "Clients" / client_folder_name(client)


def lost_root_path(client: Client) -> Path:
    return practice_files_root() / "Lost Clients" / client_folder_name(client)


def years_in_name(name: str) -> List[int]:
    found = [int(y) for y in YEAR_RE.findall(name or "")]
    return [y for y in found if 2000 <= y <= 2100]


def classify_working_paper_year(filename: str, current_year: int) -> Optional[int]:
    years = years_in_name(filename)
    if not years:
        return None
    return years[-1]


def save_playbook(
    db: Session,
    client: Client,
    *,
    bookkeeping_source: str = "xero",
    source_org_id: str = "",
    source_notes: str = "",
    iris_client_code: str = "",
    iris_notes: str = "",
    year_end_month: Optional[int] = None,
    year_end_day: Optional[int] = None,
    current_year: Optional[int] = None,
    approver_name: str = "",
    approver_email: str = "",
    approval_notes: str = "",
    quirks: str = "",
    write_pack: bool = True,
) -> Tuple[ClientPlaybook, Dict[str, Any]]:
    row = get_or_create_playbook(db, client.id)
    src = (bookkeeping_source or "xero").strip().lower()
    row.bookkeeping_source = src if src in SOURCE_CODES else "xero"
    row.source_org_id = (source_org_id or "").strip() or None
    row.source_notes = (source_notes or "").strip() or None
    row.iris_client_code = (iris_client_code or "").strip() or None
    row.iris_notes = (iris_notes or "").strip() or None
    row.year_end_month = year_end_month if year_end_month else None
    row.year_end_day = year_end_day if year_end_day else None
    row.current_year = current_year if current_year else None
    row.approver_name = (approver_name or "").strip() or None
    row.approver_email = (approver_email or "").strip() or None
    row.approval_notes = (approval_notes or "").strip() or None
    row.quirks = (quirks or "").strip() or None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    result: Dict[str, Any] = {}
    if write_pack:
        result = ensure_client_pack(db, client, playbook=row, move_prior_years=True)
    return row, result


def render_agents_md(
    db: Session, client: Client, playbook: Optional[ClientPlaybook] = None
) -> str:
    pb = playbook or get_playbook(db, client.id)
    ye_m = (pb.year_end_month if pb else None) or None
    ye_d = (pb.year_end_day if pb else None) or 31
    ye = ""
    if ye_m:
        ye = f"{int(ye_d or 31):02d}/{int(ye_m):02d}"
    year = current_accounts_year(db, client, pb)
    code = (pb.bookkeeping_source if pb else "xero") or "xero"
    src = source_label(code)
    if code == "sage50":
        pull_line = (
            "- Pull: Sage 50 is desktop — export trial balance / nominal / bank and drop the files into `Current/Source` (Playbook → Upload source file)."
        )
        post_line = (
            "- Journals: prepare in Excel, post back inside Sage 50. Accologise cannot log into Sage 50."
        )
    elif code == "client_tb":
        pull_line = (
            "- Pull: client (or their bookkeeper) emails a ready trial balance / bank CSV. "
            "Those land in Outlook folder Holding. File into `Current/Source` "
            "(Playbook → Upload source file, or Import from Holding)."
        )
        post_line = (
            "- IRIS: recode the supplied TB to the IRIS Elements chart. Send drafts for approval, "
            "post any changes, submit accounts/tax through IRIS, then send finals. "
            "Nothing is posted back to Xero/Sage/QBO."
        )
    elif code == "bank_csv":
        pull_line = "- Pull: drop bank CSVs into `Current/Source` (Playbook → Upload source file)."
        post_line = "- Journals: not posted to a bookkeeping app — keep workings in `Current/Journals`."
    elif code == "qbo":
        pull_line = "- Pull: Playbook → Pull from QuickBooks into `Current/Source`."
        post_line = "- Journals: draft in `Current/Journals`, review, confirm, then post to QuickBooks."
    elif code == "sage_cloud":
        if is_sales_ledger_only(pb):
            pull_line = (
                "- Pull: Playbook → Pull Sage sales ledger into `Current/Source` "
                "(sales invoices, credit notes, customers, receipts, aged debtors). "
                "Bank is not in Sage — use statements or screenshots in `Current/Source`."
            )
            post_line = (
                "- Journals: do not post year-end journals to Sage. "
                "Sage is the sales ledger only. Keep YE journals in `Current/Journals` "
                "and import the recoded trial balance to IRIS."
            )
        else:
            pull_line = "- Pull: Playbook → Pull from Sage into `Current/Source`."
            post_line = "- Journals: draft in `Current/Journals`, review, confirm, then post to Sage."
    else:
        pull_line = "- Pull: Playbook → Pull from Xero (trial balance, bank, nominal) into `Current/Source`."
        post_line = "- Journals: draft in `Current/Journals`, review, confirm, then post to Xero."
    name = client.display_name() if hasattr(client, "display_name") else client.company_name
    people = []
    try:
        people = list(client.people or [])
    except Exception:
        people = []
    contacts = ", ".join(
        (p.display_name() if hasattr(p, "display_name") else p.full_name) or ""
        for p in people[:12]
        if p
    )
    lines = [
        f"# {name}",
        "",
        "Practice playbook for Accologise automation. Read this before starting a job for this client.",
        (
            "Do not invent figures. Recode only from the supplied trial balance. "
            "Confirm with the client before submitting through IRIS."
            if code == "client_tb"
            else (
                "Do not invent figures, company numbers, or journals. "
                "Confirm before submitting through IRIS. Do not post year-end journals to Sage."
                if is_sales_ledger_only(pb)
                else f"Do not invent figures, company numbers, or journals. Confirm before posting anything back to {src} or IRIS."
            )
        ),
        "",
        "## Identity",
        f"- CRM client id: {client.id}",
        f"- Company number: {client.company_number or '—'}",
        f"- Type: {client.client_type or '—'}",
        f"- Status: {client.overall_status or 'Active'}",
        f"- Year end: {ye or '—'} (current accounts year {year})",
        f"- VAT: {client.vat_number or '—'} / {client.vat_frequency or 'none'}",
        f"- UTR: {client.utr or '—'}",
        "",
        "## Books",
        f"- Source: {src}",
        f"- Source org / tenant id: {(pb.source_org_id if pb else None) or '—'}",
        f"- Source notes: {_clean_source_notes(pb.source_notes if pb else None, code)}",
        pull_line,
        post_line,
        "",
        "## Working papers",
        "- This year: `Current/Working Papers`",
        "- Prior years: `Current/YYYY`",
        "- IRIS-ready trial balance: `Current/IRIS Import`",
        "- Standing statutory files stay in Accounts / Tax Return / ID-KYC at the client root.",
        "",
        "## IRIS Elements",
        f"- IRIS client code: {(pb.iris_client_code if pb else None) or '—'}",
        f"- IRIS notes: {(pb.iris_notes if pb else None) or '—'}",
        "- Produce Excel working papers first, then import the trial balance into IRIS.",
        "- IRIS produces statutory accounts, CT computation, and files at Companies House / HMRC after client approval.",
        "",
        "## Approval",
        f"- Approver: {(pb.approver_name if pb else None) or client.contact_name or '—'}",
        f"- Email: {(pb.approver_email if pb else None) or client.email or '—'}",
        f"- Notes: {(pb.approval_notes if pb else None) or '—'}",
        "",
        "## People",
        f"- {contacts or '—'}",
        "",
        "## Quirks",
        (pb.quirks if pb and pb.quirks else "- None recorded yet."),
        "",
        "## Do not",
        "- Do not invent journals or overwrite source books without confirmation.",
        *(
            [
                "- Do not post year-end journals to Sage, and do not treat a Sage trial balance as the books (sales ledger only).",
            ]
            if is_sales_ledger_only(pb)
            else []
        ),
        "- Do not file at Companies House or HMRC without client approval.",
        "- Do not move this client to Lost Clients unless they are disengaged.",
        "",
    ]
    return "\n".join(lines)


def _clean_source_notes(notes: Optional[str], code: str) -> str:
    text = (notes or "").strip()
    if not text:
        return "—"
    # Drop the first-pass "left as Xero default" line when Excel later set another source.
    if code != "xero":
        text = re.sub(
            r"Source detected from working papers: xero\.[^.]*\.",
            "",
            text,
            flags=re.I,
        )
    text = re.sub(r"\s{2,}", " ", text).strip(" .")
    return text or "—"


def set_bookkeeping_source(
    db: Session, client: Client, source: str, *, note: str = ""
) -> ClientPlaybook:
    src = (source or "xero").strip().lower()
    if src not in SOURCE_CODES:
        src = "xero"
    pb = get_or_create_playbook(db, client.id)
    pb.bookkeeping_source = src
    if note:
        pb.source_notes = note.strip()
    pb.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pb)
    try:
        root = client_root_path(client)
        root.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text(render_agents_md(db, client, pb), encoding="utf-8")
    except OSError:
        pass
    return pb


def list_source_rows(db: Session, source: str = "") -> List[Dict[str, Any]]:
    q = live_clients_query(db).order_by(Client.company_name)
    rows = []
    want = (source or "").strip().lower()
    for client in q.all():
        pb = get_playbook(db, client.id)
        code = (pb.bookkeeping_source if pb else None) or "xero"
        if want and code != want:
            continue
        rows.append(
            {
                "id": client.id,
                "name": client.display_name() if hasattr(client, "display_name") else client.company_name,
                "company_number": client.company_number or "",
                "source": code,
                "source_label": source_label(code),
                "org": (pb.source_org_id if pb else None) or "",
                "notes": _clean_source_notes(pb.source_notes if pb else None, code),
            }
        )
    return rows


def upload_source_file(db: Session, client: Client, filename: str, content: bytes) -> Tuple[str, str]:
    """Save an exported TB / bank / nominal file into Current/Source."""
    from app.services.xero_books import _source_dir, SAFE_NAME

    if not content:
        return "", "Empty file."
    name = Path(filename or "source.csv").name
    name = SAFE_NAME.sub("-", name).strip("-") or "source.csv"
    folder = _source_dir(db, client)
    dest = folder / name
    dest.write_bytes(content)
    return str(dest), ""


def rewrite_all_agents_md(db: Session) -> int:
    n = 0
    for client in live_clients_query(db).all():
        pb = get_or_create_playbook(db, client.id)
        try:
            root = client_root_path(client)
            root.mkdir(parents=True, exist_ok=True)
            (root / "AGENTS.md").write_text(render_agents_md(db, client, pb), encoding="utf-8")
            n += 1
        except OSError:
            continue
    db.commit()
    return n


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def ensure_client_pack(
    db: Session,
    client: Client,
    *,
    playbook: Optional[ClientPlaybook] = None,
    move_prior_years: bool = True,
    commit: bool = True,
) -> Dict[str, Any]:
    """Create Current / year folders and write AGENTS.md. Optionally file prior-year papers."""
    root = practice_files_root()
    result: Dict[str, Any] = {
        "ok": False,
        "root": str(root),
        "client_dir": "",
        "created": [],
        "moved": [],
        "skipped_move": "",
        "agents_md": "",
        "error": "",
        "current_year": None,
    }
    if not root.exists():
        result["error"] = f"Practice files root not found: {root}"
        return result

    pb = playbook or get_or_create_playbook(db, client.id)
    year = current_accounts_year(db, client, pb)
    result["current_year"] = year
    client_dir = client_root_path(client)
    result["client_dir"] = str(client_dir)
    _mkdir(client_dir)

    created: List[str] = []
    current = client_dir / "Current"
    if not current.exists():
        _mkdir(current)
        created.append("Current")
    for sub in CURRENT_PACK_SUBFOLDERS:
        p = current / sub
        if not p.exists():
            _mkdir(p)
            created.append(f"Current/{sub}")
    for standing in STANDING_CATEGORIES:
        p = client_dir / standing
        if not p.exists():
            _mkdir(p)
            created.append(standing)

    moved: List[str] = []
    if move_prior_years:
        moved, skip = _file_working_papers_by_year(client_dir, year)
        result["skipped_move"] = skip
        result["moved"] = moved

    md_path = client_dir / "AGENTS.md"
    md_path.write_text(render_agents_md(db, client, pb), encoding="utf-8")
    result["agents_md"] = str(md_path)

    pb.folders_ensured_at = datetime.utcnow()
    pb.agents_md_written_at = datetime.utcnow()
    pb.updated_at = datetime.utcnow()
    if commit:
        db.commit()

    result["created"] = created
    result["ok"] = True
    return result


def _file_working_papers_by_year(client_dir: Path, current_year: int) -> Tuple[List[str], str]:
    """Move year-tagged files from root Working Papers into Current/{year} or Current/Working Papers."""
    wp = client_dir / "Working Papers"
    if not wp.is_dir():
        return [], ""
    files = [p for p in wp.iterdir() if p.is_file() and not p.name.startswith("~$")]
    if len(files) > MOVE_CAP:
        return [], f"Left {len(files)} files in Working Papers (over {MOVE_CAP} — review by hand)."
    moved: List[str] = []
    for src in files:
        year = classify_working_paper_year(src.name, current_year)
        if year is None:
            continue
        if year >= current_year:
            dest_dir = client_dir / "Current" / "Working Papers"
        else:
            dest_dir = client_dir / "Current" / str(year)
        _mkdir(dest_dir)
        dest = dest_dir / src.name
        if dest.exists():
            continue
        try:
            shutil.move(str(src), str(dest))
            moved.append(f"{src.name} → {dest_dir.relative_to(client_dir)}")
        except OSError:
            continue
    return moved, ""


def ensure_live_client_packs(
    db: Session,
    *,
    move_prior_years: bool = True,
    client_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    q = live_clients_query(db).order_by(Client.company_name)
    if client_ids:
        q = q.filter(Client.id.in_(list(client_ids)))
    clients = q.all()
    ok = 0
    failed = 0
    moved_n = 0
    errors: List[str] = []
    for client in clients:
        try:
            res = ensure_client_pack(
                db, client, move_prior_years=move_prior_years, commit=False
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"{client.company_name}: {exc}")
            continue
        if res.get("ok"):
            ok += 1
            moved_n += len(res.get("moved") or [])
        else:
            failed += 1
            if res.get("error"):
                errors.append(f"{client.company_name}: {res['error']}")
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        errors.append(f"commit: {exc}")
    return {
        "clients": len(clients),
        "ok": ok,
        "failed": failed,
        "moved": moved_n,
        "errors": errors[:30],
        "root": str(practice_files_root()),
    }


def playbook_summary(db: Session, client: Client) -> Dict[str, Any]:
    pb = get_playbook(db, client.id)
    year = current_accounts_year(db, client, pb)
    root = client_root_path(client)
    xero_tenants: List[Dict[str, Any]] = []
    xero_connected = False
    sage_status: Dict[str, Any] = {}
    qbo_status: Dict[str, Any] = {}
    as_at = None
    drafts: List[Dict[str, Any]] = []
    try:
        from app.services.book_oauth import connection_status as book_status
        from app.services.xero_books import default_as_at, list_journal_drafts
        from app.services.xero_oauth import connection_status

        xs = connection_status(db)
        xero_connected = bool(xs.get("connected"))
        xero_tenants = list(xs.get("tenants") or [])
        sage_status = book_status(db, "sage")
        qbo_status = book_status(db, "qbo")
        as_at = default_as_at(db, client, pb)
        if root.is_dir():
            drafts = list_journal_drafts(db, client)
    except Exception:
        pass
    return {
        "playbook": pb,
        "sources": BOOKKEEPING_SOURCES,
        "current_year": year,
        "client_dir": str(root),
        "client_dir_exists": root.is_dir(),
        "agents_md": str(root / "AGENTS.md") if (root / "AGENTS.md").is_file() else "",
        "current_dir": str(root / "Current") if (root / "Current").is_dir() else "",
        "source_label": source_label(pb.bookkeeping_source if pb else None),
        "xero_connected": xero_connected,
        "xero_tenants": xero_tenants,
        "sage_status": sage_status,
        "qbo_status": qbo_status,
        "as_at": as_at.isoformat() if as_at else "",
        "journal_drafts": drafts,
        "sales_ledger_only": is_sales_ledger_only(pb),
    }
