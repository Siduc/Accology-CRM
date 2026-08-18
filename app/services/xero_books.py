"""Pull Xero books into Current/Source and post reviewed journals back."""

from __future__ import annotations

import csv
import io
import json
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

import app.config as _cfg
from app.models.client import Client
from app.models.client_playbook import ClientPlaybook
from app.services import client_connections
from app.services.client_playbook import (
    client_root_path,
    current_accounts_year,
    ensure_client_pack,
    get_or_create_playbook,
)
from app.services.xero_oauth import (
    _http_json,
    get_valid_access_token,
    parse_tenants,
)

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _api_base() -> str:
    return (_cfg.XERO_API_BASE or "https://api.xero.com/api.xro/2.0").rstrip("/")


def xero_get(
    access_token: str,
    tenant_id: str,
    path: str,
    *,
    params: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Any, str]:
    url = path if path.startswith("http") else f"{_api_base()}{path}"
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return _http_json("GET", url, access_token, tenant_id=tenant_id)


def xero_put(
    access_token: str,
    tenant_id: str,
    path: str,
    payload: Dict[str, Any],
) -> Tuple[bool, Any, str]:
    url = path if path.startswith("http") else f"{_api_base()}{path}"
    body = json.dumps(payload).encode("utf-8")
    return _http_json("PUT", url, access_token, tenant_id=tenant_id, data=body)


def default_as_at(db: Session, client: Client, playbook: Optional[ClientPlaybook] = None) -> date:
    pb = playbook or get_or_create_playbook(db, client.id)
    year = current_accounts_year(db, client, pb)
    month = int(pb.year_end_month or 12)
    day = int(pb.year_end_day or 31)
    month = min(12, max(1, month))
    last = monthrange(year, month)[1]
    return date(year, month, min(max(1, day), last))


def resolve_tenant_id(db: Session, client: Client, playbook: Optional[ClientPlaybook] = None) -> Tuple[str, str]:
    """Return (tenant_id, error). Prefers playbook, then Xero connection, then unique tenant."""
    pb = playbook or get_or_create_playbook(db, client.id)
    tid = (pb.source_org_id or "").strip()
    if tid:
        return tid, ""
    conn = client_connections.get_connection(db, client.id, "xero")
    if conn and conn.enabled and (conn.external_id or "").strip():
        return conn.external_id.strip(), ""
    _, err, row = get_valid_access_token(db)
    if err:
        return "", err
    tenants = parse_tenants(row)
    if len(tenants) == 1:
        return str(tenants[0].get("tenantId") or ""), ""
    if not tenants:
        return "", "Xero is connected but no organisations were returned. Reconnect Xero."
    return "", "Choose the Xero organisation on the Playbook tab (Source org)."


def assign_tenant(db: Session, client: Client, tenant_id: str, tenant_name: str = "") -> None:
    pb = get_or_create_playbook(db, client.id)
    pb.source_org_id = (tenant_id or "").strip() or None
    if tenant_name:
        note = (pb.source_notes or "").strip()
        tag = f"Xero org: {tenant_name}"
        if tag not in note:
            pb.source_notes = f"{tag}. {note}".strip() if note else tag
    if not pb.bookkeeping_source or pb.bookkeeping_source == "other":
        pb.bookkeeping_source = "xero"
    pb.updated_at = datetime.utcnow()
    client_connections.set_connection(
        db,
        client.id,
        "xero",
        enabled=bool(tenant_id),
        external_id=(tenant_id or "").strip() or None,
        notes=tenant_name or None,
    )
    db.commit()


def tenant_label(db: Session, tenant_id: str) -> str:
    _, _, row = get_valid_access_token(db)
    for t in parse_tenants(row):
        if str(t.get("tenantId") or "") == tenant_id:
            return str(t.get("tenantName") or tenant_id)
    return tenant_id


def _source_dir(db: Session, client: Client) -> Path:
    ensure_client_pack(db, client, move_prior_years=False, commit=True)
    path = client_root_path(client) / "Current" / "Source"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _journals_dir(db: Session, client: Client) -> Path:
    ensure_client_pack(db, client, move_prior_years=False, commit=True)
    path = client_root_path(client) / "Current" / "Journals"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(path: Path, headers: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in headers})


def _flatten_report(report: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    header_cells: List[str] = []

    def walk(rows: List[Any], section: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            rtype = (row.get("RowType") or "").strip()
            title = (row.get("Title") or section or "").strip()
            if rtype == "Header":
                header_cells.clear()
                for cell in row.get("Cells") or []:
                    header_cells.append(str((cell or {}).get("Value") or "").strip() or "Col")
                continue
            if rtype == "Section":
                out.extend(walk(row.get("Rows") or [], title))
                continue
            cells = row.get("Cells") or []
            rec: Dict[str, Any] = {"Section": title}
            for i, cell in enumerate(cells):
                key = header_cells[i] if i < len(header_cells) else f"Col{i+1}"
                rec[key] = str((cell or {}).get("Value") or "")
            if any(v for k, v in rec.items() if k != "Section"):
                out.append(rec)
        return out

    reports = report.get("Reports") if isinstance(report, dict) else None
    if not reports and isinstance(report, dict):
        reports = [report]
    rows: List[Dict[str, Any]] = []
    for rep in reports or []:
        if isinstance(rep, dict):
            rows.extend(walk(rep.get("Rows") or []))
    headers = ["Section"]
    for rec in rows:
        for k in rec:
            if k not in headers:
                headers.append(k)
    return headers, rows


def _page_list(
    access_token: str,
    tenant_id: str,
    path: str,
    key: str,
    *,
    params: Optional[Dict[str, str]] = None,
    max_pages: int = 20,
) -> Tuple[List[Dict[str, Any]], str]:
    items: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        q = dict(params or {})
        q["page"] = str(page)
        ok, data, err = xero_get(access_token, tenant_id, path, params=q)
        if not ok:
            return items, err
        chunk = []
        if isinstance(data, dict):
            chunk = data.get(key) or []
        if not chunk:
            break
        items.extend(r for r in chunk if isinstance(r, dict))
        if len(chunk) < 100:
            break
    return items, ""


def pull_client_books(
    db: Session,
    client: Client,
    *,
    as_at: Optional[date] = None,
) -> Dict[str, Any]:
    """Write trial balance, chart, bank, and existing manuals into Current/Source."""
    pb = get_or_create_playbook(db, client.id)
    as_at = as_at or default_as_at(db, client, pb)
    tenant_id, terr = resolve_tenant_id(db, client, pb)
    result: Dict[str, Any] = {
        "ok": False,
        "as_at": as_at.isoformat(),
        "tenant_id": tenant_id,
        "tenant_name": "",
        "files": [],
        "counts": {},
        "error": "",
        "source_dir": "",
    }
    if terr:
        result["error"] = terr
        return result

    token, err, _row = get_valid_access_token(db)
    if not token:
        result["error"] = err or "Xero is not connected."
        return result

    result["tenant_name"] = tenant_label(db, tenant_id)
    source = _source_dir(db, client)
    result["source_dir"] = str(source)
    stamp = as_at.isoformat()
    files: List[str] = []
    counts: Dict[str, int] = {}

    ok, tb, err = xero_get(
        token, tenant_id, "/Reports/TrialBalance", params={"date": stamp}
    )
    if ok and isinstance(tb, dict):
        headers, rows = _flatten_report(tb)
        name = f"{stamp} Trial Balance.csv"
        _write_csv(source / name, headers or ["Account"], rows)
        files.append(name)
        counts["trial_balance"] = len(rows)
    elif err:
        counts["trial_balance_error"] = err

    for report_name, fname in (
        ("ProfitAndLoss", f"{stamp} Profit and Loss.csv"),
        ("BalanceSheet", f"{stamp} Balance Sheet.csv"),
    ):
        rok, raw, rerr = xero_get(
            token, tenant_id, f"/Reports/{report_name}", params={"date": stamp}
        )
        if rok and isinstance(raw, dict):
            headers, rows = _flatten_report(raw)
            _write_csv(source / fname, headers or ["Account"], rows)
            files.append(fname)
            counts[report_name] = len(rows)
        elif rerr:
            counts[f"{report_name}_error"] = rerr

    ok, accounts, err = xero_get(token, tenant_id, "/Accounts")
    if ok and isinstance(accounts, dict):
        rows = []
        for acc in accounts.get("Accounts") or []:
            if not isinstance(acc, dict):
                continue
            rows.append(
                {
                    "Code": acc.get("Code") or "",
                    "Name": acc.get("Name") or "",
                    "Type": acc.get("Type") or "",
                    "Status": acc.get("Status") or "",
                    "TaxType": acc.get("TaxType") or "",
                    "Class": acc.get("Class") or "",
                    "Description": acc.get("Description") or "",
                }
            )
        name = f"{stamp} Chart of Accounts.csv"
        _write_csv(
            source / name,
            ["Code", "Name", "Type", "Class", "Status", "TaxType", "Description"],
            rows,
        )
        files.append(name)
        counts["accounts"] = len(rows)
    elif err:
        counts["accounts_error"] = err

    start = date(as_at.year - 1, as_at.month, as_at.day) + timedelta(days=1)
    where = (
        f"Date>=DateTime({start.year},{start.month:02d},{start.day:02d}) "
        f"AND Date<=DateTime({as_at.year},{as_at.month:02d},{as_at.day:02d})"
    )
    bank_rows, bank_err = _page_list(
        token,
        tenant_id,
        "/BankTransactions",
        "BankTransactions",
        params={"where": where},
    )
    bank_out = []
    for tx in bank_rows:
        bank = tx.get("BankAccount") or {}
        contact = tx.get("Contact") or {}
        bank_out.append(
            {
                "Date": (tx.get("Date") or "")[:10],
                "Type": tx.get("Type") or "",
                "Status": tx.get("Status") or "",
                "BankAccount": bank.get("Name") or "",
                "Contact": contact.get("Name") or "",
                "Reference": tx.get("Reference") or "",
                "Total": tx.get("Total") or "",
                "IsReconciled": tx.get("IsReconciled") or "",
                "BankTransactionID": tx.get("BankTransactionID") or "",
            }
        )
    name = f"{stamp} Bank Transactions.csv"
    _write_csv(
        source / name,
        [
            "Date",
            "Type",
            "Status",
            "BankAccount",
            "Contact",
            "Reference",
            "Total",
            "IsReconciled",
            "BankTransactionID",
        ],
        bank_out,
    )
    files.append(name)
    counts["bank_transactions"] = len(bank_out)
    if bank_err:
        counts["bank_transactions_error"] = bank_err

    manuals, man_err = _page_list(token, tenant_id, "/ManualJournals", "ManualJournals")
    man_out = []
    for jn in manuals:
        lines = jn.get("JournalLines") or []
        if not lines:
            man_out.append(
                {
                    "Date": (jn.get("Date") or "")[:10],
                    "Narration": jn.get("Narration") or "",
                    "Status": jn.get("Status") or "",
                    "AccountCode": "",
                    "Description": "",
                    "LineAmount": "",
                    "ManualJournalID": jn.get("ManualJournalID") or "",
                }
            )
        for line in lines:
            if not isinstance(line, dict):
                continue
            man_out.append(
                {
                    "Date": (jn.get("Date") or "")[:10],
                    "Narration": jn.get("Narration") or "",
                    "Status": jn.get("Status") or "",
                    "AccountCode": line.get("AccountCode") or "",
                    "Description": line.get("Description") or "",
                    "LineAmount": line.get("LineAmount") or "",
                    "ManualJournalID": jn.get("ManualJournalID") or "",
                }
            )
    name = f"{stamp} Manual Journals.csv"
    _write_csv(
        source / name,
        [
            "Date",
            "Narration",
            "Status",
            "AccountCode",
            "Description",
            "LineAmount",
            "ManualJournalID",
        ],
        man_out,
    )
    files.append(name)
    counts["manual_journals"] = len(manuals)
    if man_err:
        counts["manual_journals_error"] = man_err

    meta = {
        "pulled_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "client_id": client.id,
        "client": client.company_name,
        "tenant_id": tenant_id,
        "tenant_name": result["tenant_name"],
        "as_at": stamp,
        "counts": counts,
        "files": files,
    }
    (source / f"{stamp} pull.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    files.append(f"{stamp} pull.json")

    result["ok"] = True
    result["files"] = files
    result["counts"] = counts
    return result


def list_journal_drafts(db: Session, client: Client) -> List[Dict[str, Any]]:
    folder = _journals_dir(db, client)
    out: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.csv")):
        posted = path.with_suffix(".posted.json")
        out.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%d/%m/%Y %H:%M"
                ),
                "posted": posted.is_file(),
            }
        )
    return out


def _money(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("£", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_journal_csv(content: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    Group CSV rows into journals.

    Columns (any case): Date, Narration, AccountCode, Description,
    plus either LineAmount or Debit + Credit.
    """
    sample = content.lstrip("\ufeff")
    try:
        reader = csv.DictReader(io.StringIO(sample))
    except csv.Error as exc:
        return [], f"Could not read CSV: {exc}"
    if not reader.fieldnames:
        return [], "CSV has no header row."

    def col(*names: str) -> Optional[str]:
        lower = { (n or "").strip().lower(): n for n in reader.fieldnames or [] }
        for name in names:
            if name.lower() in lower:
                return lower[name.lower()]
        return None

    c_date = col("date")
    c_narr = col("narration", "journal", "memo")
    c_code = col("accountcode", "account", "code")
    c_desc = col("description", "line")
    c_amt = col("lineamount", "amount")
    c_dr = col("debit", "dr")
    c_cr = col("credit", "cr")
    if not c_date or not c_narr or not c_code:
        return [], "CSV needs Date, Narration and AccountCode columns."

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for raw in reader:
        d = (raw.get(c_date) or "").strip()
        narr = (raw.get(c_narr) or "").strip()
        code = (raw.get(c_code) or "").strip()
        if not d or not narr or not code:
            continue
        if c_amt:
            amount = _money(raw.get(c_amt))
        else:
            amount = _money(raw.get(c_dr) if c_dr else 0) - _money(
                raw.get(c_cr) if c_cr else 0
            )
        if abs(amount) < 0.0001:
            continue
        grouped.setdefault((d, narr), []).append(
            {
                "AccountCode": code,
                "Description": (raw.get(c_desc) or "").strip() if c_desc else "",
                "LineAmount": round(amount, 2),
            }
        )

    journals: List[Dict[str, Any]] = []
    for (d, narr), lines in grouped.items():
        total = round(sum(ln["LineAmount"] for ln in lines), 2)
        journals.append(
            {
                "Date": d,
                "Narration": narr,
                "Lines": lines,
                "LineCount": len(lines),
                "Net": total,
                "Balanced": abs(total) < 0.02,
            }
        )
    if not journals:
        return [], "No journal lines found in the CSV."
    return journals, ""


def load_journal_draft(db: Session, client: Client, filename: str) -> Tuple[Path, List[Dict[str, Any]], str]:
    safe = Path(filename).name
    if not safe.lower().endswith(".csv"):
        return Path(), [], "Only CSV journal drafts are accepted."
    path = _journals_dir(db, client) / safe
    if not path.is_file():
        return path, [], f"Draft not found: {safe}"
    text = path.read_text(encoding="utf-8-sig")
    journals, err = parse_journal_csv(text)
    return path, journals, err


def save_uploaded_draft(db: Session, client: Client, filename: str, content: bytes) -> Tuple[str, str]:
    name = Path(filename or "journal.csv").name
    if not name.lower().endswith(".csv"):
        name = f"{name}.csv"
    name = SAFE_NAME.sub("-", name).strip("-") or "journal.csv"
    if not name.lower().endswith(".csv"):
        name += ".csv"
    path = _journals_dir(db, client) / name
    path.write_bytes(content)
    _, err = parse_journal_csv(content.decode("utf-8-sig", errors="replace"))
    return name, err


def post_journal_draft(
    db: Session,
    client: Client,
    filename: str,
    *,
    status: str = "DRAFT",
) -> Dict[str, Any]:
    """Post a reviewed CSV to Xero. status is DRAFT or POSTED."""
    result: Dict[str, Any] = {
        "ok": False,
        "filename": filename,
        "status": status,
        "posted": [],
        "error": "",
    }
    path, journals, err = load_journal_draft(db, client, filename)
    if err:
        result["error"] = err
        return result
    unbalanced = [j for j in journals if not j["Balanced"]]
    if unbalanced:
        result["error"] = (
            f"{len(unbalanced)} journal(s) do not balance. Fix the CSV before posting."
        )
        return result

    pb = get_or_create_playbook(db, client.id)
    tenant_id, terr = resolve_tenant_id(db, client, pb)
    if terr:
        result["error"] = terr
        return result
    token, tok_err, _ = get_valid_access_token(db)
    if not token:
        result["error"] = tok_err or "Xero is not connected."
        return result

    want = (status or "DRAFT").strip().upper()
    if want not in ("DRAFT", "POSTED"):
        want = "DRAFT"

    payload = {
        "ManualJournals": [
            {
                "Narration": j["Narration"][:4000],
                "Date": j["Date"],
                "Status": want,
                "LineAmountTypes": "NoTax",
                "JournalLines": [
                    {
                        "LineAmount": ln["LineAmount"],
                        "AccountCode": ln["AccountCode"],
                        "Description": (ln["Description"] or j["Narration"])[:4000],
                    }
                    for ln in j["Lines"]
                ],
            }
            for j in journals
        ]
    }
    ok, data, perr = xero_put(token, tenant_id, "/ManualJournals", payload)
    if not ok:
        result["error"] = perr or "Xero rejected the journals."
        return result

    posted = []
    for jn in (data or {}).get("ManualJournals") or []:
        if isinstance(jn, dict):
            posted.append(
                {
                    "ManualJournalID": jn.get("ManualJournalID"),
                    "Narration": jn.get("Narration"),
                    "Status": jn.get("Status"),
                    "Date": jn.get("Date"),
                }
            )
    sidecar = {
        "posted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "tenant_id": tenant_id,
        "requested_status": want,
        "journals": posted,
        "filename": path.name,
    }
    path.with_suffix(".posted.json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )
    result["ok"] = True
    result["posted"] = posted
    return result
