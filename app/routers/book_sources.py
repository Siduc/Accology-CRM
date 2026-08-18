"""Sage Business Cloud and QuickBooks Online OAuth, pull, journals."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client
from app.services.book_oauth import (
    add_qbo_realm,
    build_authorise_url,
    configured,
    connection_status,
    exchange_code,
    fetch_sage_businesses,
    get_valid_access_token,
    parse_state,
    resolve_redirect,
    revoke_local,
    save_token,
    sign_state,
)
from app.services.xero_books import load_journal_draft, save_uploaded_draft
from app.templating import render

router = APIRouter(tags=["book-sources"])


def _mods(provider: str):
    if provider == "sage":
        from app.services import sage_books as books

        return books
    if provider == "qbo":
        from app.services import qbo_books as books

        return books
    raise ValueError(provider)


def _settings(provider: str) -> str:
    return f"/settings/{provider}"


@router.get("/oauth/{provider}/start")
async def oauth_start(provider: str, request: Request, return_to: str = ""):
    if provider not in ("sage", "qbo"):
        return RedirectResponse("/settings", status_code=303)
    if not configured(provider):
        return RedirectResponse(
            f"{_settings(provider)}?oauth_error="
            + url_quote(f"{provider.upper()} is not configured. Add client id and secret to .env."),
            status_code=303,
        )
    ret = (return_to or "").strip() or _settings(provider)
    if not ret.startswith("/"):
        ret = _settings(provider)
    redir = resolve_redirect(provider, request)
    state = sign_state(provider=provider, return_to=ret, redirect_uri=redir)
    try:
        url = build_authorise_url(provider, state=state, redirect_uri=redir)
    except RuntimeError as exc:
        return RedirectResponse(
            f"{_settings(provider)}?oauth_error={url_quote(str(exc)[:300])}",
            status_code=303,
        )
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    realmId: str = "",
    db: Session = Depends(get_db),
):
    if provider not in ("sage", "qbo"):
        return RedirectResponse("/settings", status_code=303)
    dest = _settings(provider)
    if error:
        return RedirectResponse(
            f"{dest}?oauth_error={url_quote((error_description or error)[:300])}",
            status_code=303,
        )
    ok, payload, serr = parse_state(state or "")
    ret = dest
    redir = ""
    if ok:
        ret = payload.get("ret") or dest
        redir = (payload.get("redir") or "").strip()
        if payload.get("p") and payload.get("p") != provider:
            return RedirectResponse(
                f"{dest}?oauth_error={url_quote('State provider mismatch.')}",
                status_code=303,
            )
    if not ok:
        return RedirectResponse(f"{dest}?oauth_error={url_quote(serr)}", status_code=303)
    if not code.strip():
        return RedirectResponse(
            f"{dest}?oauth_error={url_quote('No authorisation code returned.')}",
            status_code=303,
        )
    if not redir:
        redir = resolve_redirect(provider, request)
    result = exchange_code(provider, code.strip(), redirect_uri=redir)
    if not result.ok:
        return RedirectResponse(
            f"{dest}?oauth_error={url_quote(result.error[:300])}",
            status_code=303,
        )
    tenants = []
    if provider == "sage":
        tenants, _ = fetch_sage_businesses(result.access_token)
    save_token(db, provider, result, tenants=tenants)
    if provider == "qbo" and realmId.strip():
        add_qbo_realm(db, realmId.strip())
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=connected", status_code=303)


@router.post("/oauth/{provider}/disconnect")
async def oauth_disconnect(provider: str, db: Session = Depends(get_db)):
    if provider in ("sage", "qbo"):
        revoke_local(db, provider)
    return RedirectResponse(f"{_settings(provider)}?oauth_msg=disconnected", status_code=303)


@router.get("/settings/sage", response_class=HTMLResponse)
async def settings_sage(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "settings_book_source.html",
        {
            "status": connection_status(db, "sage"),
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "help": (
                "Create an app at developer.sage.com (Accounting API). "
                "Redirect URI must be exactly http://localhost:8000/oauth/sage/callback. "
                "Then on the client Playbook tab, link the Sage business and pull. "
                "If Sage is sales ledger only, the pull is invoices, credit notes, customers and debtors — not a trial balance. "
                "Sage 50 desktop cannot connect this way — export a CSV into Current/Source instead."
            ),
        },
    )


@router.get("/settings/qbo", response_class=HTMLResponse)
async def settings_qbo(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "settings_book_source.html",
        {
            "status": connection_status(db, "qbo"),
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "help": (
                "Create an app at developer.intuit.com. "
                "Redirect URI must be exactly http://localhost:8000/oauth/qbo/callback. "
                "Each Connect adds one QuickBooks company (realm)."
            ),
        },
    )


@router.post("/settings/{provider}/refresh-orgs")
async def refresh_orgs(provider: str, db: Session = Depends(get_db)):
    if provider == "sage":
        token, err, row = get_valid_access_token(db, "sage")
        if not token or not row:
            return RedirectResponse(
                f"/settings/sage?oauth_error={url_quote(err or 'Not connected')}",
                status_code=303,
            )
        tenants, terr = fetch_sage_businesses(token)
        if tenants:
            import json

            row.tenants_json = json.dumps(tenants)
            db.commit()
        if terr and not tenants:
            return RedirectResponse(
                f"/settings/sage?oauth_error={url_quote(terr[:300])}",
                status_code=303,
            )
    return RedirectResponse(f"{_settings(provider)}?oauth_msg=orgs", status_code=303)


def _client(db: Session, client_id: int):
    c = db.query(Client).filter(Client.id == client_id).first()
    return c


@router.post("/clients/{client_id:int}/{provider}/assign")
async def assign_org(
    client_id: int,
    provider: str,
    tenant_id: str = Form(""),
    db: Session = Depends(get_db),
):
    client = _client(db, client_id)
    if not client or provider not in ("sage", "qbo"):
        return RedirectResponse("/clients", status_code=303)
    status = connection_status(db, provider)
    name = ""
    for t in status.get("tenants") or []:
        if str(t.get("tenantId")) == tenant_id.strip():
            name = str(t.get("tenantName") or "")
    books = _mods(provider)
    if provider == "sage":
        books.assign_business(db, client, tenant_id.strip(), name)
    else:
        books.assign_realm(db, client, tenant_id.strip(), name)
    return RedirectResponse(f"/clients/{client_id}?tab=playbook&saved=playbook", status_code=303)


@router.post("/clients/{client_id:int}/{provider}/pull")
async def pull_books(
    client_id: int,
    provider: str,
    as_at: str = Form(""),
    db: Session = Depends(get_db),
):
    client = _client(db, client_id)
    if not client or provider not in ("sage", "qbo"):
        return RedirectResponse("/clients", status_code=303)
    when = None
    if (as_at or "").strip():
        try:
            when = datetime.strptime(as_at.strip(), "%Y-%m-%d").date()
        except ValueError:
            when = None
    res = _mods(provider).pull_client_books(db, client, as_at=when)
    if not res.get("ok"):
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
            + url_quote((res.get("error") or "Pull failed")[:400]),
            status_code=303,
        )
    n = res.get("counts") or {}
    if res.get("sales_ledger_only"):
        msg = (
            f"Pulled Sage sales ledger to Current/Source as at {res.get('as_at')}. "
            f"Invoices {n.get('sales_invoices', 0)}, "
            f"credit notes {n.get('sales_credit_notes', 0)}, "
            f"customers {n.get('contacts', 0)}, "
            f"receipts {n.get('contact_payments', 0)}."
        )
    else:
        msg = (
            f"Pulled {provider.upper()} to Current/Source as at {res.get('as_at')}. "
            f"TB {n.get('trial_balance', 0)} rows. "
            f"Invoices {n.get('sales_invoices', 0)}."
        )
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg={url_quote(msg[:400])}",
        status_code=303,
    )


@router.post("/clients/{client_id:int}/{provider}/journals/upload")
async def upload_journal(
    client_id: int,
    provider: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    client = _client(db, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    raw = await file.read()
    name, err = save_uploaded_draft(db, client, file.filename or "journal.csv", raw)
    if err:
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg={url_quote(err[:400])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/clients/{client_id}/{provider}/journals/{url_quote(name)}",
        status_code=303,
    )


@router.get("/clients/{client_id:int}/{provider}/journals/{filename}", response_class=HTMLResponse)
async def review_journal(
    client_id: int,
    provider: str,
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
):
    client = _client(db, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    path, journals, err = load_journal_draft(db, client, filename)
    return render(
        request,
        "xero/journal_review.html",
        {
            "client": client,
            "filename": path.name or filename,
            "journals": journals,
            "error": err or request.query_params.get("err", ""),
            "all_balanced": bool(journals) and all(j.get("Balanced") for j in journals),
            "post_action": f"/clients/{client.id}/{provider}/journals/{path.name or filename}/post",
        },
    )


@router.post("/clients/{client_id:int}/{provider}/journals/{filename}/post")
async def confirm_post(
    client_id: int,
    provider: str,
    filename: str,
    status: str = Form("DRAFT"),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    client = _client(db, client_id)
    if not client or provider not in ("sage", "qbo", "xero"):
        return RedirectResponse("/clients", status_code=303)
    if (confirm or "").strip().lower() not in ("yes", "1", "on", "true"):
        return RedirectResponse(
            f"/clients/{client_id}/{provider}/journals/{url_quote(filename)}"
            f"?err={url_quote('Tick confirm before posting.')}",
            status_code=303,
        )
    if provider == "xero":
        from app.services.xero_books import post_journal_draft as post_fn
    else:
        post_fn = _mods(provider).post_journal_draft
    res = post_fn(db, client, filename, status=status)
    if not res.get("ok"):
        return RedirectResponse(
            f"/clients/{client_id}/{provider}/journals/{url_quote(filename)}"
            f"?err={url_quote((res.get('error') or 'Post failed')[:400])}",
            status_code=303,
        )
    n = len(res.get("posted") or [])
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
        + url_quote(f"Posted {n} journal(s) to {provider.upper()}."),
        status_code=303,
    )


@router.get("/settings/book-sources", response_class=HTMLResponse)
async def book_sources_list(
    request: Request,
    source: str = "",
    db: Session = Depends(get_db),
):
    from app.models.client_playbook import BOOKKEEPING_SOURCES
    from app.services.client_playbook import list_source_rows

    all_rows = list_source_rows(db)
    counts: dict = {}
    for row in all_rows:
        counts[row["source"]] = counts.get(row["source"], 0) + 1
    rows = [r for r in all_rows if r["source"] == source] if source else all_rows
    return render(
        request,
        "settings_book_sources.html",
        {
            "rows": rows,
            "sources": BOOKKEEPING_SOURCES,
            "filter_source": source,
            "counts": counts,
            "total": len(all_rows),
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/settings/book-sources/{client_id:int}")
async def book_sources_set(
    client_id: int,
    bookkeeping_source: str = Form("xero"),
    source: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.client_playbook import set_bookkeeping_source

    client = _client(db, client_id)
    if not client:
        return RedirectResponse("/settings/book-sources", status_code=303)
    set_bookkeeping_source(
        db,
        client,
        bookkeeping_source,
        note=f"Source set in CRM books list ({bookkeeping_source}).",
    )
    q = f"?source={url_quote(source)}" if source else ""
    return RedirectResponse(
        f"/settings/book-sources{q}&msg={url_quote('Updated ' + (client.company_name or ''))}"
        if q
        else f"/settings/book-sources?msg={url_quote('Updated ' + (client.company_name or ''))}",
        status_code=303,
    )


@router.post("/clients/{client_id:int}/source/upload")
async def upload_source_export(
    client_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    from app.services.client_playbook import upload_source_file

    client = _client(db, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    raw = await file.read()
    dest, err = upload_source_file(db, client, file.filename or "source.csv", raw)
    if err:
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg={url_quote(err[:400])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
        + url_quote(f"Saved to Current/Source: {PathName(dest)}"),
        status_code=303,
    )


def PathName(path: str) -> str:
    from pathlib import Path

    return Path(path).name
