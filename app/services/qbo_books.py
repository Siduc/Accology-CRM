"""QuickBooks Online pull + journal post into the client Current pack."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

import app.config as _cfg
from app.models.client import Client
from app.services import client_connections
from app.services.book_oauth import get_valid_access_token, http_json, parse_tenants
from app.services.client_playbook import get_or_create_playbook
from app.services.xero_books import (
    _source_dir,
    _write_csv,
    default_as_at,
    load_journal_draft,
)

PROVIDER = "qbo"


def _api_root() -> str:
    env = (_cfg.QBO_ENVIRONMENT or "production").lower()
    if env in ("sandbox", "dev"):
        return "https://sandbox-quickbooks.api.intuit.com/v3"
    return (_cfg.QBO_API_BASE or "https://quickbooks.api.intuit.com/v3").rstrip("/")


def _headers() -> Dict[str, str]:
    return {"Accept": "application/json"}


def _get(token: str, realm: str, path: str, params: Optional[Dict[str, str]] = None) -> tuple:
    url = path if path.startswith("http") else f"{_api_root()}/company/{realm}{path}"
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    extra = dict(_headers())
    extra["Accept"] = "application/json"
    return http_json("GET", url, token, extra_headers=extra)


def _post(token: str, realm: str, path: str, payload: Dict[str, Any]) -> tuple:
    url = path if path.startswith("http") else f"{_api_root()}/company/{realm}{path}"
    extra = dict(_headers())
    extra["Content-Type"] = "application/json"
    extra["Accept"] = "application/json"
    return http_json("POST", url, token, extra_headers=extra, data=json.dumps(payload).encode("utf-8"))


def resolve_realm(db: Session, client: Client) -> tuple:
    pb = get_or_create_playbook(db, client.id)
    tid = (pb.source_org_id or "").strip()
    if tid:
        return tid, ""
    conn = client_connections.get_connection(db, client.id, "qbo")
    if conn and conn.enabled and conn.external_id:
        return conn.external_id.strip(), ""
    token, err, row = get_valid_access_token(db, PROVIDER)
    if err:
        return "", err or "QuickBooks is not connected."
    tenants = parse_tenants(row)
    if len(tenants) == 1:
        return str(tenants[0].get("tenantId") or ""), ""
    return "", "Choose the QuickBooks company on the Playbook tab."


def assign_realm(db: Session, client: Client, realm_id: str, name: str = "") -> None:
    pb = get_or_create_playbook(db, client.id)
    pb.source_org_id = (realm_id or "").strip() or None
    pb.bookkeeping_source = "qbo"
    pb.updated_at = datetime.utcnow()
    client_connections.set_connection(
        db, client.id, "qbo", enabled=bool(realm_id), external_id=realm_id or None, notes=name or None
    )
    db.commit()


def pull_client_books(db: Session, client: Client, *, as_at: Optional[date] = None) -> Dict[str, Any]:
    pb = get_or_create_playbook(db, client.id)
    as_at = as_at or default_as_at(db, client, pb)
    start = date(as_at.year - 1, as_at.month, as_at.day) + timedelta(days=1)
    realm, err = resolve_realm(db, client)
    result: Dict[str, Any] = {
        "ok": False,
        "as_at": as_at.isoformat(),
        "tenant_id": realm,
        "files": [],
        "counts": {},
        "error": "",
        "source_dir": "",
    }
    if err:
        result["error"] = err
        return result
    token, terr, _ = get_valid_access_token(db, PROVIDER)
    if not token:
        result["error"] = terr or "QuickBooks is not connected."
        return result
    source = _source_dir(db, client)
    result["source_dir"] = str(source)
    stamp = as_at.isoformat()
    files: List[str] = []
    counts: Dict[str, Any] = {}
    minor = {"minorversion": _cfg.QBO_MINOR_VERSION or "75"}

    ok, tb, e = _get(
        token,
        realm,
        "/reports/TrialBalance",
        {"end_date": stamp, **minor},
    )
    if ok and isinstance(tb, dict):
        rows = []
        for row in (tb.get("Rows") or {}).get("Row") or []:
            if not isinstance(row, dict):
                continue
            cols = [c.get("value") for c in (row.get("ColData") or []) if isinstance(c, dict)]
            if cols:
                rows.append(
                    {
                        "Account": cols[0] if len(cols) > 0 else "",
                        "Debit": cols[1] if len(cols) > 1 else "",
                        "Credit": cols[2] if len(cols) > 2 else "",
                    }
                )
        name = f"{stamp} QBO Trial Balance.csv"
        _write_csv(source / name, ["Account", "Debit", "Credit"], rows)
        files.append(name)
        counts["trial_balance"] = len(rows)
    elif e:
        counts["trial_balance_error"] = e

    query = quote("select * from Account maxresults 1000")
    ok, acc, e = _get(token, realm, f"/query?query={query}", minor)
    if ok and isinstance(acc, dict):
        qresp = acc.get("QueryResponse") or {}
        rows = []
        for item in qresp.get("Account") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "Id": item.get("Id") or "",
                    "AcctNum": item.get("AcctNum") or "",
                    "Name": item.get("Name") or "",
                    "AccountType": item.get("AccountType") or "",
                    "CurrentBalance": item.get("CurrentBalance") or "",
                    "Active": item.get("Active"),
                }
            )
        name = f"{stamp} QBO Chart of Accounts.csv"
        _write_csv(
            source / name,
            ["Id", "AcctNum", "Name", "AccountType", "CurrentBalance", "Active"],
            rows,
        )
        files.append(name)
        counts["accounts"] = len(rows)
    elif e:
        counts["accounts_error"] = e

    for report, label in (("ProfitAndLoss", "Profit and Loss"), ("BalanceSheet", "Balance Sheet")):
        ok, raw, e = _get(
            token,
            realm,
            f"/reports/{report}",
            {"end_date": stamp, "start_date": start.isoformat(), **minor},
        )
        if ok and isinstance(raw, dict):
            rows = []
            for row in (raw.get("Rows") or {}).get("Row") or []:
                if not isinstance(row, dict):
                    continue
                cols = [c.get("value") for c in (row.get("ColData") or []) if isinstance(c, dict)]
                if cols:
                    rec = {f"Col{i+1}": v for i, v in enumerate(cols)}
                    rows.append(rec)
            headers = []
            for rec in rows:
                for k in rec:
                    if k not in headers:
                        headers.append(k)
            name = f"{stamp} QBO {label}.csv"
            _write_csv(source / name, headers or ["Col1"], rows)
            files.append(name)
            counts[report] = len(rows)
        elif e:
            counts[f"{report}_error"] = e

    meta = {
        "pulled_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "provider": "qbo",
        "client_id": client.id,
        "realm_id": realm,
        "as_at": stamp,
        "counts": counts,
        "files": files,
    }
    (source / f"{stamp} qbo pull.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    files.append(f"{stamp} qbo pull.json")
    result.update(ok=True, files=files, counts=counts)
    return result


def post_journal_draft(db: Session, client: Client, filename: str, *, status: str = "DRAFT") -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "filename": filename, "posted": [], "error": ""}
    path, journals, err = load_journal_draft(db, client, filename)
    if err:
        result["error"] = err
        return result
    if any(not j["Balanced"] for j in journals):
        result["error"] = "One or more journals do not balance."
        return result
    realm, rerr = resolve_realm(db, client)
    if rerr:
        result["error"] = rerr
        return result
    token, terr, _ = get_valid_access_token(db, PROVIDER)
    if not token:
        result["error"] = terr or "QuickBooks is not connected."
        return result
    posted = []
    for j in journals:
        payload = {
            "TxnDate": j["Date"],
            "PrivateNote": j["Narration"][:4000],
            "Line": [
                {
                    "Amount": abs(float(ln["LineAmount"])),
                    "DetailType": "JournalEntryLineDetail",
                    "Description": ln["Description"] or j["Narration"],
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit" if ln["LineAmount"] > 0 else "Credit",
                        "AccountRef": {"value": str(ln["AccountCode"])},
                    },
                }
                for ln in j["Lines"]
            ],
        }
        ok, data, perr = _post(token, realm, "/journalentry", payload)
        if not ok:
            result["error"] = perr or "QuickBooks rejected a journal."
            result["posted"] = posted
            return result
        je = (data or {}).get("JournalEntry") or data or {}
        posted.append({"Narration": j["Narration"], "Id": je.get("Id")})
    path.with_suffix(".posted.json").write_text(
        json.dumps(
            {
                "posted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "provider": "qbo",
                "journals": posted,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    result.update(ok=True, posted=posted)
    return result
