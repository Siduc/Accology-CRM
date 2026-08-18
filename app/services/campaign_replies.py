"""Campaign reply routing: payroll@ mailbox + CRM prospect update."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.email_message import EmailMessage
from app.models.prospecting import CampaignMember, Prospect, ProspectActivity, ProspectCampaign
from app.services.ms_graph_mail import _request
from app.services.prospecting import log_activity

logger = logging.getLogger("accountant_crm.campaign_replies")

PAYROLL_MAILBOX = "payroll@accology.co"
MELISSA_MAILBOX = "melissa@accology.co"
PRACTICE_DOMAINS = ("accology.co", "accologise.co")

_INTERESTED = re.compile(
    r"\b(yes|go ahead|please (set|set up|proceed|do)|happy to|count me in|interested)\b",
    re.I,
)
_DECLINED = re.compile(r"\b(no thanks|not interested|don't bother|do not|leave it)\b", re.I)


def default_reply_to(campaign: Optional[ProspectCampaign] = None) -> str:
    if campaign and (campaign.reply_to_email or "").strip():
        return campaign.reply_to_email.strip()
    return PAYROLL_MAILBOX


def _norm(addr: str) -> str:
    return (addr or "").strip().lower()


def _addrs_from_recipients(rows: Any) -> List[str]:
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ea = row.get("emailAddress") or {}
        addr = _norm((ea.get("address") if isinstance(ea, dict) else "") or "")
        if addr:
            out.append(addr)
    return out


def _is_practice(addr: str) -> bool:
    a = _norm(addr)
    return any(a.endswith("@" + d) or a.endswith("." + d) for d in PRACTICE_DOMAINS)


def graph_get_user(token: str, upn: str) -> Tuple[Optional[Dict[str, Any]], str]:
    ok, data, err, status = _request(
        "GET",
        f"/users/{upn}?$select=id,displayName,mail,userPrincipalName,accountEnabled,userType",
        token,
    )
    if ok and isinstance(data, dict) and data.get("id"):
        return data, ""
    return None, err or f"HTTP {status}"


def graph_find_user(token: str, query: str) -> List[Dict[str, Any]]:
    from urllib.parse import quote

    q = (query or "").replace("'", "''")
    filt = quote(f"startswith(mail,'{q}') or startswith(userPrincipalName,'{q}')", safe="")
    ok, data, err, _ = _request(
        "GET",
        "/users?$select=id,displayName,mail,userPrincipalName"
        f"&$filter={filt}&$top=10",
        token,
    )
    if ok and isinstance(data, dict):
        return [r for r in (data.get("value") or []) if isinstance(r, dict)]
    logger.info("graph find user %s: %s", query, err)
    return []


def try_create_shared_mailbox(
    token: str,
    *,
    upn: str = PAYROLL_MAILBOX,
    display_name: str = "Accology Payroll",
    share_with: str = MELISSA_MAILBOX,
) -> Dict[str, Any]:
    """
    Best-effort: find or create payroll@ and grant Melissa access.
    Delegated Mail.* tokens usually cannot create mailboxes — we report that.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "created": False,
        "mailbox": upn,
        "user": None,
        "shared_with": share_with,
        "permission": "",
        "error": "",
        "steps": [],
    }
    existing, err = graph_get_user(token, upn)
    if existing:
        result["user"] = {
            "id": existing.get("id"),
            "mail": existing.get("mail") or existing.get("userPrincipalName"),
            "name": existing.get("displayName"),
        }
        result["steps"].append("Mailbox already exists in the directory.")
    else:
        result["steps"].append(f"Lookup {upn}: {err or 'not found'}")
        import secrets

        password = "Pa#" + secrets.token_urlsafe(18)
        payload = {
            "accountEnabled": False,
            "displayName": display_name,
            "mailNickname": upn.split("@")[0],
            "userPrincipalName": upn,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": False,
                "password": password,
            },
            "usageLocation": "GB",
        }
        ok, data, cerr, status = _request(
            "POST",
            "/users",
            token,
            data=json.dumps(payload).encode("utf-8"),
        )
        if ok and isinstance(data, dict) and data.get("id"):
            result["created"] = True
            result["user"] = {
                "id": data.get("id"),
                "mail": data.get("mail") or data.get("userPrincipalName") or upn,
                "name": data.get("displayName"),
            }
            result["steps"].append("Created directory user (needs Exchange mailbox / shared conversion).")
        else:
            result["error"] = (
                cerr
                or f"Could not create {upn} (HTTP {status}). "
                "Microsoft 365 admin is required to create a shared mailbox."
            )
            result["steps"].append(result["error"])
            return result

    # Grant Melissa Full Access if we have an id.
    mailbox_id = (result.get("user") or {}).get("id")
    melissa, merr = graph_get_user(token, share_with)
    if not melissa:
        found = graph_find_user(token, share_with.split("@")[0])
        melissa = found[0] if found else None
        if not melissa:
            result["steps"].append(f"Could not resolve {share_with}: {merr or 'not found'}")
    if mailbox_id and melissa:
        mid = melissa.get("id")
        # Graph beta mailbox permission
        grant = {
            "grantedToUser": mid,
            "roles": ["fullAccess", "sendAs"],
        }
        ok, data, perr, status = _request(
            "POST",
            f"/users/{mailbox_id}/mailboxSettings",
            token,
            data=json.dumps({"delegateMeetingMessageDeliveryOptions": "sendToDelegateAndInformationToPrincipal"}).encode(
                "utf-8"
            ),
        )
        # Try the Exchange admin mailboxPermissions endpoint
        ok2, data2, perr2, status2 = _request(
            "POST",
            f"/users/{upn}/permissionGrants",
            token,
            data=json.dumps(
                {
                    "clientId": "AccologiseCRM",
                    "consentType": "Principal",
                    "principalId": mid,
                }
            ).encode("utf-8"),
        )
        if ok2:
            result["permission"] = "granted"
            result["steps"].append(f"Permission grant posted for {share_with}.")
        else:
            result["steps"].append(
                f"Could not grant mailbox access to {share_with} via Graph "
                f"(HTTP {status2}: {(perr2 or '')[:180]}). "
                "Add Melissa as a member of the shared mailbox in Microsoft 365 admin "
                "(Recipients → Shared mailboxes → payroll → Members)."
            )
    result["ok"] = bool(result.get("user"))
    return result


def list_payroll_messages(token: str, *, days: int = 90, top: int = 50) -> Tuple[List[Dict[str, Any]], str]:
    """Messages addressed to payroll@ — shared mailbox first, then signed-in inbox search."""
    collected: List[Dict[str, Any]] = []
    seen = set()
    err_last = ""

    from urllib.parse import quote

    select = (
        "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
        "bodyPreview,conversationId,internetMessageId,webLink,isDraft"
    )
    ok, data, err, _ = _request(
        "GET",
        f"/users/{PAYROLL_MAILBOX}/messages?$top={int(top)}"
        f"&$select={quote(select, safe=',')}"
        f"&$orderby={quote('receivedDateTime desc', safe='')}",
        token,
    )
    if ok and isinstance(data, dict):
        for row in data.get("value") or []:
            if isinstance(row, dict) and row.get("id") and row["id"] not in seen:
                seen.add(row["id"])
                collected.append(row)
    else:
        err_last = err or ""

    since = (datetime.utcnow() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    search = quote(f"to:{PAYROLL_MAILBOX} received>={since}", safe=":@")
    ok2, data2, err2, _ = _request(
        "GET",
        "/me/messages?$top="
        + str(int(top))
        + f"&$select={quote(select, safe=',')}"
        + f"&$search=%22{search}%22",
        token,
    )
    if ok2 and isinstance(data2, dict):
        for row in data2.get("value") or []:
            if isinstance(row, dict) and row.get("id") and row["id"] not in seen:
                seen.add(row["id"])
                collected.append(row)
    elif not collected:
        err_last = err2 or err_last

    return collected, err_last


def _already_logged(db: Session, graph_id: str) -> bool:
    if not graph_id:
        return False
    row = (
        db.query(ProspectActivity.id)
        .filter(ProspectActivity.meta_json.isnot(None))
        .filter(ProspectActivity.meta_json.contains(graph_id))
        .first()
    )
    return bool(row)


def _find_prospect(db: Session, from_addr: str, campaign: Optional[ProspectCampaign]) -> Optional[Prospect]:
    addr = _norm(from_addr)
    if not addr:
        return None
    if campaign:
        members = (
            db.query(CampaignMember)
            .filter(
                CampaignMember.campaign_id == campaign.id,
                CampaignMember.status != "removed",
            )
            .all()
        )
        for m in members:
            p = m.prospect
            if p and _norm(p.email or "") == addr:
                return p
    return (
        db.query(Prospect)
        .filter(Prospect.email.ilike(addr))
        .order_by(Prospect.id.desc())
        .first()
    )


def _outcome(preview: str) -> str:
    text = preview or ""
    if _DECLINED.search(text):
        return "declined"
    if _INTERESTED.search(text):
        return "interested"
    return "replied"


def ingest_payroll_replies(db: Session, token: str, *, campaign_id: Optional[int] = None) -> Dict[str, Any]:
    """Pull payroll@ replies and update matching campaign prospects."""
    result = {
        "ok": False,
        "fetched": 0,
        "logged": 0,
        "skipped": 0,
        "unmatched": 0,
        "error": "",
        "unmatched_from": [],
    }
    messages, err = list_payroll_messages(token)
    result["fetched"] = len(messages)
    if err and not messages:
        result["error"] = err
        return result

    campaign = None
    if campaign_id:
        campaign = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if campaign is None:
        campaign = (
            db.query(ProspectCampaign)
            .filter(ProspectCampaign.reply_to_email.ilike(PAYROLL_MAILBOX))
            .order_by(ProspectCampaign.id.desc())
            .first()
        )
        if campaign is None:
            campaign = (
                db.query(ProspectCampaign)
                .filter(ProspectCampaign.name.ilike("%personal allowance payroll%"))
                .first()
            )

    for msg in messages:
        if msg.get("isDraft"):
            result["skipped"] += 1
            continue
        graph_id = str(msg.get("id") or "")
        if graph_id and _already_logged(db, graph_id):
            result["skipped"] += 1
            continue
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        from_addr = _norm(frm.get("address") or "")
        from_name = (frm.get("name") or "").strip()
        if not from_addr or _is_practice(from_addr):
            result["skipped"] += 1
            continue
        to_addrs = _addrs_from_recipients(msg.get("toRecipients"))
        cc_addrs = _addrs_from_recipients(msg.get("ccRecipients"))
        targets = set(to_addrs + cc_addrs)
        if PAYROLL_MAILBOX not in targets and campaign_id is None:
            # still accept if it is a reply we pulled from the payroll mailbox
            pass
        prospect = _find_prospect(db, from_addr, campaign)
        if not prospect:
            result["unmatched"] += 1
            if from_addr not in result["unmatched_from"]:
                result["unmatched_from"].append(from_addr)
            continue
        preview = (msg.get("bodyPreview") or "").strip()
        subject = (msg.get("subject") or "").strip() or "(no subject)"
        received = None
        raw_dt = msg.get("receivedDateTime") or ""
        if raw_dt:
            try:
                received = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                received = None
        outcome = _outcome(preview)
        log_activity(
            db,
            prospect.id,
            activity_type="email",
            subject=f"Reply to payroll@: {subject[:180]}",
            body=(
                f"From: {from_name} <{from_addr}>\n"
                f"To: {', '.join(to_addrs) or PAYROLL_MAILBOX}\n\n"
                f"{preview[:4000]}"
            ),
            direction="inbound",
            outcome=outcome,
            campaign_id=campaign.id if campaign else None,
            activity_at=received,
            meta={
                "graph_id": graph_id,
                "conversation_id": msg.get("conversationId") or "",
                "internet_message_id": msg.get("internetMessageId") or "",
                "web_link": msg.get("webLink") or "",
                "mailbox": PAYROLL_MAILBOX,
            },
            commit=False,
        )
        if campaign:
            member = (
                db.query(CampaignMember)
                .filter(
                    CampaignMember.campaign_id == campaign.id,
                    CampaignMember.prospect_id == prospect.id,
                )
                .first()
            )
            if member and (member.status or "queued") in ("queued", "sent"):
                member.status = "responded"
                member.last_touch_at = datetime.utcnow()
                member.notes = (
                    ((member.notes or "").strip() + "\n" if member.notes else "")
                    + f"Reply {received.isoformat(timespec='minutes') if received else 'now'} ({outcome})"
                ).strip()
        prospect.next_step = "Payroll campaign reply received — follow up"
        if outcome == "interested":
            prospect.next_step = "Payroll campaign — wants Accology Pays, set up PAYE"
        elif outcome == "declined":
            prospect.next_step = "Payroll campaign — declined, no chase"
        prospect.updated_at = datetime.utcnow()

        from app.services.paye_onboarding import apply_onboarding, parse_reply_body

        parsed = parse_reply_body(preview)
        if parsed and prospect.client_id:
            from app.models.client import Client

            cl = db.query(Client).filter(Client.id == prospect.client_id).first()
            if cl:
                parsed["source"] = "email_reply"
                apply_onboarding(db, cl, parsed, prospect=prospect)

        if prospect.client_id:
            em = EmailMessage(
                client_id=prospect.client_id,
                direction="inbound",
                to_address=PAYROLL_MAILBOX,
                subject=subject[:500],
                body=preview[:8000] or None,
                status="logged",
                provider="graph",
                graph_message_id=graph_id or None,
                internet_message_id=(msg.get("internetMessageId") or None),
                conversation_id=(msg.get("conversationId") or None),
                sent_by=from_addr,
                sent_at=received,
            )
            db.add(em)
        result["logged"] += 1

    db.commit()
    result["ok"] = True
    return result
