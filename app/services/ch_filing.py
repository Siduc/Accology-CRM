"""Companies House API Filing helpers — prepare CS (transactions + readiness)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.models import Client
from app.models.cs_pack import CsPack
from app.services.ch_oauth import (
    api_base,
    get_valid_access_token,
    latest_token_for_client,
    oauth_is_ready,
)
from app.services.company_numbers import normalize_company_number
from app.services.cs_automation import form_dict


@dataclass
class FilingHttpResult:
    ok: bool
    status_code: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _bearer_json(
    method: str,
    path: str,
    access_token: str,
    body: Optional[Dict[str, Any]] = None,
) -> FilingHttpResult:
    url = f"{api_base()}{path}"
    data_bytes = None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data_bytes, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            return FilingHttpResult(
                ok=True,
                status_code=getattr(resp, "status", 200) or 200,
                data=parsed if isinstance(parsed, dict) else {"value": parsed},
            )
    except HTTPError as exc:
        try:
            err_raw = exc.read().decode("utf-8", errors="replace")
            err_data = json.loads(err_raw) if err_raw else {}
        except Exception:
            err_data = {}
            err_raw = str(exc)
        msg = ""
        if isinstance(err_data, dict):
            msg = (
                err_data.get("error")
                or err_data.get("message")
                or err_data.get("errors")
                or ""
            )
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg[:5])
        return FilingHttpResult(
            ok=False,
            status_code=exc.code,
            data=err_data if isinstance(err_data, dict) else {},
            error=f"HTTP {exc.code}: {msg or err_raw[:240]}",
        )
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return FilingHttpResult(ok=False, error=str(exc))


def create_transaction(
    access_token: str, company_number: str, *, reference: str = ""
) -> FilingHttpResult:
    cn = normalize_company_number(company_number) or ""
    payload: Dict[str, Any] = {}
    if cn:
        payload["company_number"] = cn
    if reference:
        payload["reference"] = reference[:50]
    return _bearer_json("POST", "/transactions", access_token, payload)


def get_transaction(access_token: str, transaction_id: str) -> FilingHttpResult:
    tid = (transaction_id or "").strip()
    if not tid:
        return FilingHttpResult(ok=False, error="Missing transaction id.")
    return _bearer_json("GET", f"/transactions/{tid}", access_token)


def build_cs_filing_payload_preview(pack: CsPack) -> Dict[str, Any]:
    """Structured preview for a future CS filing resource (not submitted)."""
    form = form_dict(pack)
    return {
        "kind": "confirmation_statement_preview",
        "note": (
            "Companies House has not published a public third-party Confirmation "
            "Statement filing resource on the Manipulate Company Data API. "
            "This payload is a practice preview only — not an electronic submission."
        ),
        "company_number": pack.company_number or form.get("company_number"),
        "company_name": form.get("company_name"),
        "made_up_to": (
            pack.made_up_to.isoformat()
            if pack.made_up_to
            else form.get("cs_made_up_to")
        ),
        "due_on": pack.due_on.isoformat() if pack.due_on else form.get("cs_due"),
        "registered_office": form.get("registered_office"),
        "sic_codes": form.get("sic_codes") or [],
        "officers": form.get("officers") or [],
        "pscs": form.get("pscs") or [],
        "confirmed_no_changes": pack.confirmed_no_changes,
        "review_notes": pack.review_notes,
        "pack_status": pack.status,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def filing_readiness(
    pack: CsPack,
    client: Optional[Client],
    *,
    oauth_ready: bool,
    has_token: bool,
    token_fresh: bool,
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []

    def add(key: str, ok: bool, label: str, detail: str = "") -> None:
        items.append({"key": key, "ok": ok, "label": label, "detail": detail})

    cn = normalize_company_number(
        (pack.company_number or (client.company_number if client else "") or "")
    )
    add(
        "company_number",
        bool(cn)
        and not (cn or "").upper().startswith("IND-")
        and not (cn or "").upper().startswith("PENDING"),
        "Valid company number",
        cn or "missing",
    )
    add(
        "oauth_config",
        oauth_ready,
        "OAuth client configured (client_id / secret)",
        "Set CH_OAUTH_CLIENT_ID and CH_OAUTH_CLIENT_SECRET in .env" if not oauth_ready else "",
    )
    add(
        "oauth_token",
        has_token and token_fresh,
        "Active OAuth token for this company",
        (
            "Authorise this company with Companies House"
            if not has_token
            else ("Token expired — re-authorise" if not token_fresh else "Connected")
        ),
    )
    auth_on_file = bool(client and (client.ch_authentication_code or "").strip())
    add(
        "auth_code",
        auth_on_file,
        "Company authentication code on client record",
        (
            "Stored on client (entered on CH consent when authorising)"
            if auth_on_file
            else "Add on client Details — required during CH authorisation"
        ),
    )
    ready_status = (pack.status or "") in ("ready_to_file", "filed")
    add(
        "pack_ready",
        ready_status or (pack.status == "in_review" and bool(pack.confirmed_no_changes)),
        "Pack reviewed / ready to file",
        f"Status: {pack.status or '—'}",
    )
    add(
        "cs_api",
        False,
        "Public CS filing API available",
        "Not yet published by Companies House for third-party software — use WebFiling to submit CS",
    )

    ok_count = sum(1 for i in items if i["ok"])
    blocking = [i for i in items if not i["ok"] and i["key"] != "cs_api"]
    return {
        "checklist": items,
        "ok_count": ok_count,
        "total": len(items),
        "can_prepare": len(blocking) == 0,
        "can_submit_cs": False,
        "blocking": [i["key"] for i in blocking],
    }


def prepare_cs_filing(
    db: Session,
    pack: CsPack,
    client: Optional[Client],
    *,
    create_tx: bool = True,
) -> Dict[str, Any]:
    """
    Build readiness + payload preview; optionally create an API Filing transaction
    as a connectivity smoke test (does not file a Confirmation Statement).
    """
    oauth_ready = oauth_is_ready()
    access, token_row, token_err = get_valid_access_token(
        db,
        crm_client_id=pack.client_id,
        company_number=pack.company_number
        or (client.company_number if client else None),
    )
    has_token = bool(token_row)
    token_fresh = bool(access)

    readiness = filing_readiness(
        pack,
        client,
        oauth_ready=oauth_ready,
        has_token=has_token,
        token_fresh=token_fresh,
    )
    preview = build_cs_filing_payload_preview(pack)

    tx_result: Dict[str, Any] = {"attempted": False}
    if create_tx and access and readiness.get("can_prepare"):
        cn = normalize_company_number(
            pack.company_number or (client.company_number if client else "") or ""
        )
        res = create_transaction(
            access,
            cn or "",
            reference=f"CS-pack-{pack.id}",
        )
        tx_result = {
            "attempted": True,
            "ok": res.ok,
            "status_code": res.status_code,
            "error": res.error,
            "data": res.data,
        }
        if res.ok:
            tid = (
                res.data.get("id")
                or res.data.get("transaction_id")
                or (res.data.get("transaction") or {}).get("id")
            )
            if tid:
                pack.ch_transaction_id = str(tid)
                tx_result["transaction_id"] = str(tid)
    elif create_tx and not access:
        tx_result = {
            "attempted": False,
            "ok": False,
            "error": token_err or "No access token",
        }

    if token_row:
        pack.oauth_token_id = token_row.id

    prep = {
        "prepared_at": datetime.utcnow().isoformat() + "Z",
        "readiness": readiness,
        "payload_preview": preview,
        "transaction": tx_result,
        "oauth_error": token_err or None,
        "disclaimer": (
            "Prepare does not electronically file a Confirmation Statement. "
            "Complete CS on WebFiling until CH publishes a public CS filing API."
        ),
    }
    pack.filing_prep_json = json.dumps(prep, default=str)
    pack.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pack)
    return prep


def prep_dict(pack: CsPack) -> Dict[str, Any]:
    if not pack.filing_prep_json:
        return {}
    try:
        return json.loads(pack.filing_prep_json)
    except json.JSONDecodeError:
        return {}


def readiness_for_pack(
    db: Session, pack: CsPack, client: Optional[Client]
) -> Dict[str, Any]:
    token_row = latest_token_for_client(db, pack.client_id) if pack.client_id else None
    access, row, _err = get_valid_access_token(
        db,
        crm_client_id=pack.client_id,
        company_number=pack.company_number,
    )
    return filing_readiness(
        pack,
        client,
        oauth_ready=oauth_is_ready(),
        has_token=bool(token_row or row),
        token_fresh=bool(access),
    )
