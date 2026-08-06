"""
Companies House XML Gateway — CS01 Confirmation Statement (Slice A).

Builds a GovTalk-style submission envelope from a CS pack + share register.
Export / preview is always available. Live gateway submit stays off until
presenter credentials and CH software package reference are configured and
``CH_XML_SUBMIT_LIVE=1``.

References:
  - https://www.gov.uk/guidance/using-software-to-file-your-companys-information
  - https://www.gov.uk/guidance/apply-to-file-with-companies-house-using-software
  - GovTalk envelope (http://www.govtalk.gov.uk/CM/envelope)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from app.config import (
    CH_XML_GATEWAY_TEST,
    CH_XML_GATEWAY_TEST_URL,
    CH_XML_GATEWAY_URL,
    CH_XML_PACKAGE_REFERENCE,
    CH_XML_PRESENTER_AUTH,
    CH_XML_PRESENTER_ID,
    CH_XML_PRODUCT,
    CH_XML_PRODUCT_VERSION,
    CH_XML_SUBMIT_LIVE,
    ch_xml_gateway_configured,
)
from app.models import Client
from app.models.cs_pack import CsPack
from app.services.company_numbers import normalize_company_number
from app.services.cs_automation import form_dict
from app.services.secrets_crypto import mask_secret

logger = logging.getLogger("accountant_crm.ch_xml_gateway")

GOVTALK_NS = "http://www.govtalk.gov.uk/CM/envelope"
CH_FORM_NS = "http://xmlgw.companieshouse.gov.uk/v1-0/schema"
SCHEMA_NOTE = (
    "Practice CS01 XML export (Slice A). Envelope follows GovTalk + CH Software "
    "Filing conventions. Field names map Accologise pack/share data for review. "
    "Before live submit, validate against your CH software developer schema pack "
    "and set presenter ID / auth + package reference on Render."
)


@dataclass
class XmlBuildResult:
    ok: bool
    xml: str = ""
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    readiness: Dict[str, Any] = field(default_factory=dict)


def gateway_url() -> str:
    if CH_XML_GATEWAY_TEST:
        return (CH_XML_GATEWAY_TEST_URL or CH_XML_GATEWAY_URL or "").strip()
    return (CH_XML_GATEWAY_URL or "").strip()


def presenter_id_masked() -> str:
    pid = (CH_XML_PRESENTER_ID or "").strip()
    if not pid:
        return ""
    if len(pid) <= 3:
        return "•" * len(pid)
    return "•" * max(3, len(pid) - 3) + pid[-3:]


def presenter_auth_set() -> bool:
    return bool((CH_XML_PRESENTER_AUTH or "").strip())


def gateway_status() -> Dict[str, Any]:
    """Settings / diagnostics snapshot (no secrets)."""
    return {
        "configured": ch_xml_gateway_configured(),
        "presenter_id_mask": presenter_id_masked(),
        "presenter_auth_set": presenter_auth_set(),
        "gateway_url": gateway_url(),
        "gateway_test": bool(CH_XML_GATEWAY_TEST),
        "package_reference": (CH_XML_PACKAGE_REFERENCE or "").strip() or "0000",
        "product": (CH_XML_PRODUCT or "Accologise").strip(),
        "product_version": (CH_XML_PRODUCT_VERSION or "1.0").strip(),
        "submit_live_enabled": bool(CH_XML_SUBMIT_LIVE),
        "can_submit": bool(CH_XML_SUBMIT_LIVE and ch_xml_gateway_configured()),
        "schema_note": SCHEMA_NOTE,
    }


def _iso(d: Optional[date]) -> str:
    if not d:
        return ""
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d.isoformat()


def _el(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    child = ET.SubElement(parent, tag)
    if text is not None:
        child.text = str(text)
    return child


def _split_person_name(full: str) -> Dict[str, str]:
    """Best-effort CH-style name split (Surname, Forename)."""
    s = (full or "").strip()
    if not s:
        return {"surname": "", "forename": "", "title": ""}
    # "SMITH, JOHN" / "SMITH, JOHN MICHAEL"
    if "," in s:
        left, right = s.split(",", 1)
        return {
            "surname": left.strip().title(),
            "forename": right.strip().title(),
            "title": "",
        }
    parts = s.split()
    if len(parts) == 1:
        return {"surname": parts[0].title(), "forename": "", "title": ""}
    titles = {"mr", "mrs", "miss", "ms", "dr", "sir", "lady", "lord", "prof"}
    title = ""
    if parts[0].lower().rstrip(".") in titles:
        title = parts[0].title()
        parts = parts[1:]
    if len(parts) == 1:
        return {"surname": parts[0].title(), "forename": "", "title": title}
    return {
        "surname": parts[-1].title(),
        "forename": " ".join(parts[:-1]).title(),
        "title": title,
    }


def xml_export_readiness(
    db: Session, pack: CsPack, client: Optional[Client]
) -> Dict[str, Any]:
    """Checklist for generating / eventually submitting CS01 XML."""
    items: List[Dict[str, Any]] = []

    def add(key: str, ok: bool, label: str, detail: str = "") -> None:
        items.append({"key": key, "ok": ok, "label": label, "detail": detail})

    form = form_dict(pack)
    cn = normalize_company_number(
        pack.company_number or form.get("company_number") or (client.company_number if client else "") or ""
    )
    cn_ok = bool(cn) and not cn.upper().startswith("IND-") and not cn.upper().startswith(
        "PENDING"
    )
    add("company_number", cn_ok, "Company number", cn or "missing")

    made = pack.made_up_to or _parse_date(form.get("cs_made_up_to") or form.get("made_up_to"))
    add(
        "made_up_to",
        bool(made),
        "CS made-up-to date",
        _iso(made) if made else "missing — refresh pack from CH",
    )

    auth_ok = False
    auth_detail = "No client"
    if client:
        try:
            from app.services.share_register import has_ch_auth_code

            auth_ok = has_ch_auth_code(client)
            auth_detail = "On file (encrypted)" if auth_ok else "Add on Statutory / CH tab"
        except Exception:
            auth_ok = bool((client.ch_authentication_code or "").strip())
            auth_detail = "On file" if auth_ok else "Missing"
    add("auth_code", auth_ok, "Company authentication code", auth_detail)

    shares_ok = False
    shares_detail = "No client"
    n_sh = 0
    if client:
        try:
            from app.services.share_register import is_shareholder_row, list_holdings

            holdings = list_holdings(db, client.id)
            n_sh = sum(1 for h in holdings if is_shareholder_row(h))
            verified = bool(client.share_register_verified_at)
            shares_ok = n_sh > 0  # export allowed with counts; verified preferred
            if verified and n_sh:
                shares_detail = f"Verified · {n_sh} shareholder(s)"
            elif n_sh:
                shares_detail = f"{n_sh} with counts (mark verified when happy)"
            elif holdings:
                shares_detail = f"{len(holdings)} potential — set share counts"
            else:
                shares_detail = "Seed register from CH, allocate shares"
        except Exception:
            shares_detail = "Share register unavailable"
    add("share_counts", shares_ok or not client, "Shareholdings for capital statement", shares_detail)

    pack_ok = (pack.status or "") in ("ready_to_file", "filed", "in_review", "draft")
    add(
        "pack",
        pack_ok and bool(pack.form_json or pack.ch_snapshot_json),
        "CS pack has CH data",
        f"Status: {pack.status or '—'}",
    )

    presenter_ok = ch_xml_gateway_configured()
    add(
        "presenter",
        presenter_ok,
        "XML Gateway presenter credentials",
        (
            f"Presenter {presenter_id_masked()} · test={CH_XML_GATEWAY_TEST}"
            if presenter_ok
            else "Set CH_XML_PRESENTER_ID + CH_XML_PRESENTER_AUTH on Render / .env"
        ),
    )

    # Export does not require presenter; submit does
    export_blockers = [
        i for i in items if not i["ok"] and i["key"] in ("company_number", "made_up_to", "pack")
    ]
    submit_blockers = [
        i
        for i in items
        if not i["ok"]
        and i["key"] in ("company_number", "made_up_to", "auth_code", "share_counts", "pack", "presenter")
    ]
    return {
        "checklist": items,
        "ok_count": sum(1 for i in items if i["ok"]),
        "total": len(items),
        "can_export": len(export_blockers) == 0,
        "can_submit": len(submit_blockers) == 0 and bool(CH_XML_SUBMIT_LIVE),
        "export_blockers": [i["key"] for i in export_blockers],
        "submit_blockers": [i["key"] for i in submit_blockers],
        "submit_live_flag": bool(CH_XML_SUBMIT_LIVE),
        "gateway": gateway_status(),
        "shareholder_count": n_sh,
    }


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _share_capital_blocks(db: Session, client: Optional[Client]) -> List[Dict[str, Any]]:
    if not client:
        return []
    from app.services.share_register import list_holdings, list_share_classes

    classes = list_share_classes(db, client.id)
    holdings = list_holdings(db, client.id)
    blocks = []
    for sc in classes:
        members = []
        for h in holdings:
            if h.share_class_id != sc.id:
                continue
            if h.shares is None:
                continue
            nm = _split_person_name(h.member_name or "")
            members.append(
                {
                    "name": (h.member_name or "").strip(),
                    "surname": nm["surname"],
                    "forename": nm["forename"],
                    "title": nm["title"],
                    "shares": float(h.shares or 0),
                    "member_type": (h.member_type or "individual").strip(),
                    "is_director": bool(getattr(h, "is_director", False)),
                    "is_psc": bool(getattr(h, "is_psc", False)),
                }
            )
        allocated = sum(m["shares"] for m in members)
        issued = float(sc.aggregate_shares) if sc.aggregate_shares is not None else allocated
        blocks.append(
            {
                "class_name": (sc.name or "Ordinary").strip(),
                "currency": (sc.currency or "GBP").strip() or "GBP",
                "nominal_value": float(sc.nominal_value or 1),
                "aggregate_shares": issued,
                "prescribed_particulars": (sc.rights_notes or "").strip()
                or "Full voting rights",
                "members": members,
                "allocated": allocated,
            }
        )
    # No classes but holdings exist
    if not blocks and holdings:
        members = []
        for h in holdings:
            if h.shares is None:
                continue
            nm = _split_person_name(h.member_name or "")
            members.append(
                {
                    "name": (h.member_name or "").strip(),
                    "surname": nm["surname"],
                    "forename": nm["forename"],
                    "title": nm["title"],
                    "shares": float(h.shares or 0),
                    "member_type": (h.member_type or "individual").strip(),
                    "is_director": bool(getattr(h, "is_director", False)),
                    "is_psc": bool(getattr(h, "is_psc", False)),
                }
            )
        if members:
            blocks.append(
                {
                    "class_name": "Ordinary",
                    "currency": "GBP",
                    "nominal_value": 1.0,
                    "aggregate_shares": sum(m["shares"] for m in members),
                    "prescribed_particulars": "Full voting rights",
                    "members": members,
                    "allocated": sum(m["shares"] for m in members),
                }
            )
    return blocks


def build_cs01_payload(
    db: Session, pack: CsPack, client: Optional[Client]
) -> Dict[str, Any]:
    """Structured CS01 payload (JSON twin of the XML body)."""
    form = form_dict(pack)
    cn = normalize_company_number(
        pack.company_number
        or form.get("company_number")
        or (client.company_number if client else "")
        or ""
    )
    made = pack.made_up_to or _parse_date(
        form.get("cs_made_up_to") or form.get("made_up_to")
    )
    due = pack.due_on or _parse_date(form.get("cs_due") or form.get("due_on"))
    company_name = (
        form.get("company_name")
        or (client.company_name if client else None)
        or ""
    )
    ro = form.get("registered_office") or ""
    if isinstance(ro, dict):
        ro = ", ".join(str(v) for v in ro.values() if v)
    sic = form.get("sic_codes") or []
    if isinstance(sic, str):
        sic = [s.strip() for s in sic.split(",") if s.strip()]
    officers = form.get("officers") or []
    pscs = form.get("pscs") or []
    capital = _share_capital_blocks(db, client)

    auth_present = False
    if client:
        try:
            from app.services.share_register import has_ch_auth_code

            auth_present = has_ch_auth_code(client)
        except Exception:
            auth_present = bool((client.ch_authentication_code or "").strip())

    return {
        "kind": "confirmation_statement_cs01",
        "schema_note": SCHEMA_NOTE,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pack_id": pack.id,
        "client_id": pack.client_id,
        "company_number": cn,
        "company_name": company_name,
        "made_up_to": _iso(made),
        "due_on": _iso(due),
        "registered_office": ro,
        "sic_codes": sic,
        "officers": officers,
        "pscs": pscs,
        "share_capital": capital,
        "confirmed_no_changes": pack.confirmed_no_changes,
        "review_notes": pack.review_notes,
        "company_auth_code_on_file": auth_present,
        "accept_lawful_purpose_statement": True,
    }


def build_cs01_xml(
    db: Session,
    pack: CsPack,
    client: Optional[Client],
    *,
    include_auth_code: bool = False,
    gateway_test: Optional[bool] = None,
) -> XmlBuildResult:
    """
    Build GovTalk envelope + FormSubmission CS01 body.

    Auth codes are redacted in the default export (preview/download for humans).
    Pass include_auth_code=True only for a submit path that never logs the XML.
    """
    readiness = xml_export_readiness(db, pack, client)
    if not readiness.get("can_export"):
        return XmlBuildResult(
            ok=False,
            error="Cannot export XML yet: " + ", ".join(readiness.get("export_blockers") or []),
            readiness=readiness,
        )

    payload = build_cs01_payload(db, pack, client)
    form = form_dict(pack)
    cn = payload["company_number"]
    made = payload["made_up_to"]
    test_flag = CH_XML_GATEWAY_TEST if gateway_test is None else gateway_test

    company_auth = ""
    if include_auth_code and client:
        try:
            from app.services.share_register import ch_auth_code_plain

            company_auth = (ch_auth_code_plain(client) or "").strip()
        except Exception:
            company_auth = ""
    if include_auth_code and not company_auth:
        return XmlBuildResult(
            ok=False,
            error="Company authentication code required for submission XML.",
            readiness=readiness,
            meta=payload,
        )

    # --- GovTalk root ---
    ET.register_namespace("", GOVTALK_NS)
    root = ET.Element(f"{{{GOVTALK_NS}}}GovTalkMessage")
    _el(root, f"{{{GOVTALK_NS}}}EnvelopeVersion", "2.0")

    header = _el(root, f"{{{GOVTALK_NS}}}Header")
    msg = _el(header, f"{{{GOVTALK_NS}}}MessageDetails")
    _el(msg, f"{{{GOVTALK_NS}}}Class", "CompanyData")
    _el(msg, f"{{{GOVTALK_NS}}}Qualifier", "request")
    _el(msg, f"{{{GOVTALK_NS}}}Function", "submit")
    tx_id = f"AC-{pack.id}-{uuid.uuid4().hex[:12]}"
    _el(msg, f"{{{GOVTALK_NS}}}TransactionID", tx_id)
    _el(msg, f"{{{GOVTALK_NS}}}CorrelationID", "")
    _el(msg, f"{{{GOVTALK_NS}}}Transformation", "XML")
    _el(msg, f"{{{GOVTALK_NS}}}GatewayTest", "1" if test_flag else "0")

    sender = _el(header, f"{{{GOVTALK_NS}}}SenderDetails")
    id_auth = _el(sender, f"{{{GOVTALK_NS}}}IDAuthentication")
    presenter = (CH_XML_PRESENTER_ID or "").strip() or "PRESENTER_NOT_SET"
    presenter_auth = (CH_XML_PRESENTER_AUTH or "").strip()
    if not include_auth_code:
        # Preview: never embed live presenter secret
        presenter_auth_out = "REDACTED" if presenter_auth else "PRESENTER_AUTH_NOT_SET"
    else:
        presenter_auth_out = presenter_auth or "PRESENTER_AUTH_NOT_SET"
    _el(id_auth, f"{{{GOVTALK_NS}}}SenderID", presenter)
    auth_node = _el(id_auth, f"{{{GOVTALK_NS}}}Authentication")
    _el(auth_node, f"{{{GOVTALK_NS}}}Method", "clear")
    _el(auth_node, f"{{{GOVTALK_NS}}}Role", "presenter")
    _el(auth_node, f"{{{GOVTALK_NS}}}Value", presenter_auth_out)

    details = _el(root, f"{{{GOVTALK_NS}}}GovTalkDetails")
    keys = _el(details, f"{{{GOVTALK_NS}}}Keys")
    key = _el(keys, f"{{{GOVTALK_NS}}}Key", cn)
    key.set("Type", "CompanyNumber")
    channel = _el(details, f"{{{GOVTALK_NS}}}ChannelRouting")
    ch = _el(channel, f"{{{GOVTALK_NS}}}Channel")
    _el(ch, f"{{{GOVTALK_NS}}}URI", (CH_XML_PRODUCT or "Accologise").strip())
    _el(ch, f"{{{GOVTALK_NS}}}Product", (CH_XML_PRODUCT or "Accologise").strip())
    _el(ch, f"{{{GOVTALK_NS}}}Version", (CH_XML_PRODUCT_VERSION or "1.0").strip())

    body = _el(root, f"{{{GOVTALK_NS}}}Body")
    # Form submission (CH Software Filing style)
    form_sub = ET.SubElement(body, f"{{{CH_FORM_NS}}}FormSubmission")
    form_sub.set("xmlns", CH_FORM_NS)
    auth_out = company_auth if include_auth_code else (
        "REDACTED" if readiness["checklist"] and any(
            i["key"] == "auth_code" and i["ok"] for i in readiness["checklist"]
        ) else "AUTH_CODE_NOT_ON_FILE"
    )
    _el(form_sub, f"{{{CH_FORM_NS}}}CompanyAuthenticationCode", auth_out)
    _el(
        form_sub,
        f"{{{CH_FORM_NS}}}PackageReference",
        (CH_XML_PACKAGE_REFERENCE or "0000").strip() or "0000",
    )
    form_data = _el(form_sub, f"{{{CH_FORM_NS}}}FormData")
    cs = _el(form_data, f"{{{CH_FORM_NS}}}ConfirmationStatement")
    _el(cs, f"{{{CH_FORM_NS}}}CompanyNumber", cn)
    _el(cs, f"{{{CH_FORM_NS}}}CompanyName", payload.get("company_name") or "")
    _el(cs, f"{{{CH_FORM_NS}}}StatementDate", made)
    if payload.get("due_on"):
        _el(cs, f"{{{CH_FORM_NS}}}DueDate", payload["due_on"])
    _el(
        cs,
        f"{{{CH_FORM_NS}}}AcceptLawfulPurposeStatement",
        "true" if payload.get("accept_lawful_purpose_statement") else "false",
    )
    if payload.get("confirmed_no_changes"):
        _el(
            cs,
            f"{{{CH_FORM_NS}}}ConfirmedNoChanges",
            str(payload["confirmed_no_changes"]),
        )

    # Registered office (as single block for review)
    if payload.get("registered_office"):
        ro_el = _el(cs, f"{{{CH_FORM_NS}}}RegisteredOfficeAddress")
        _el(ro_el, f"{{{CH_FORM_NS}}}AddressLine", str(payload["registered_office"])[:200])

    sic_parent = _el(cs, f"{{{CH_FORM_NS}}}SICCodes")
    for code in payload.get("sic_codes") or []:
        _el(sic_parent, f"{{{CH_FORM_NS}}}SICCode", str(code))

    # Officers (from pack download)
    off_parent = _el(cs, f"{{{CH_FORM_NS}}}Officers")
    for o in payload.get("officers") or []:
        if not isinstance(o, dict):
            continue
        oel = _el(off_parent, f"{{{CH_FORM_NS}}}Officer")
        nm = _split_person_name(o.get("name") or "")
        _el(oel, f"{{{CH_FORM_NS}}}Title", nm.get("title") or "")
        _el(oel, f"{{{CH_FORM_NS}}}Forename", nm.get("forename") or "")
        _el(oel, f"{{{CH_FORM_NS}}}Surname", nm.get("surname") or (o.get("name") or ""))
        _el(oel, f"{{{CH_FORM_NS}}}Role", o.get("role") or "Director")
        if o.get("appointed_on"):
            _el(oel, f"{{{CH_FORM_NS}}}AppointedOn", str(o["appointed_on"])[:10])

    # PSCs
    psc_parent = _el(cs, f"{{{CH_FORM_NS}}}PSCs")
    for p in payload.get("pscs") or []:
        if not isinstance(p, dict):
            continue
        pel = _el(psc_parent, f"{{{CH_FORM_NS}}}PSC")
        _el(pel, f"{{{CH_FORM_NS}}}Name", p.get("name") or "")
        _el(pel, f"{{{CH_FORM_NS}}}Kind", p.get("kind") or p.get("kind_label") or "")
        if p.get("notified_on"):
            _el(pel, f"{{{CH_FORM_NS}}}NotifiedOn", str(p["notified_on"])[:10])
        natures = p.get("natures_of_control") or []
        if natures:
            npar = _el(pel, f"{{{CH_FORM_NS}}}NaturesOfControl")
            for n in natures:
                _el(npar, f"{{{CH_FORM_NS}}}Nature", str(n))

    # Statement of capital + members from practice share register
    soc = _el(cs, f"{{{CH_FORM_NS}}}StatementOfCapital")
    for block in payload.get("share_capital") or []:
        sc_el = _el(soc, f"{{{CH_FORM_NS}}}ShareClass")
        _el(sc_el, f"{{{CH_FORM_NS}}}Class", block.get("class_name") or "Ordinary")
        _el(sc_el, f"{{{CH_FORM_NS}}}Currency", block.get("currency") or "GBP")
        _el(
            sc_el,
            f"{{{CH_FORM_NS}}}NominalValue",
            f"{float(block.get('nominal_value') or 1):.4f}".rstrip("0").rstrip("."),
        )
        _el(
            sc_el,
            f"{{{CH_FORM_NS}}}NumberAllotted",
            f"{float(block.get('aggregate_shares') or 0):.0f}",
        )
        _el(
            sc_el,
            f"{{{CH_FORM_NS}}}AggregateNominalValue",
            f"{float(block.get('aggregate_shares') or 0) * float(block.get('nominal_value') or 1):.2f}",
        )
        _el(
            sc_el,
            f"{{{CH_FORM_NS}}}PrescribedParticulars",
            (block.get("prescribed_particulars") or "")[:500],
        )
        mem_parent = _el(sc_el, f"{{{CH_FORM_NS}}}Members")
        for m in block.get("members") or []:
            mel = _el(mem_parent, f"{{{CH_FORM_NS}}}Member")
            _el(mel, f"{{{CH_FORM_NS}}}Name", m.get("name") or "")
            _el(mel, f"{{{CH_FORM_NS}}}Surname", m.get("surname") or "")
            _el(mel, f"{{{CH_FORM_NS}}}Forename", m.get("forename") or "")
            _el(mel, f"{{{CH_FORM_NS}}}SharesHeld", f"{float(m.get('shares') or 0):.0f}")
            _el(mel, f"{{{CH_FORM_NS}}}MemberType", m.get("member_type") or "individual")

    # Practice metadata (not for CH — strip before live schema lock)
    meta = _el(cs, f"{{{CH_FORM_NS}}}AccologiseMeta")
    _el(meta, f"{{{CH_FORM_NS}}}PackId", str(pack.id))
    _el(meta, f"{{{CH_FORM_NS}}}ExportMode", "preview" if not include_auth_code else "submit")
    _el(meta, f"{{{CH_FORM_NS}}}SchemaNote", SCHEMA_NOTE[:400])
    _el(meta, f"{{{CH_FORM_NS}}}GeneratedAt", payload["generated_at"])
    if form.get("company_name"):
        _el(meta, f"{{{CH_FORM_NS}}}Source", "cs_pack_form_json+share_register")

    # Pretty print
    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    xml_str = xml_bytes.decode("utf-8")

    meta_out = {
        "transaction_id": tx_id,
        "company_number": cn,
        "made_up_to": made,
        "gateway_test": test_flag,
        "include_auth_code": include_auth_code,
        "presenter_id_mask": presenter_id_masked(),
        "auth_codes_redacted": not include_auth_code,
        "byte_length": len(xml_bytes),
        "generated_at": payload["generated_at"],
        "payload": {
            k: payload[k]
            for k in (
                "company_name",
                "company_number",
                "made_up_to",
                "share_capital",
                "sic_codes",
            )
            if k in payload
        },
    }
    return XmlBuildResult(ok=True, xml=xml_str, meta=meta_out, readiness=readiness)


def save_xml_export(
    db: Session,
    pack: CsPack,
    client: Optional[Client],
) -> XmlBuildResult:
    """Build preview XML, store summary on pack, return result."""
    result = build_cs01_xml(db, pack, client, include_auth_code=False)
    if not result.ok:
        return result

    record = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "meta": result.meta,
        "readiness": {
            "can_export": result.readiness.get("can_export"),
            "can_submit": result.readiness.get("can_submit"),
            "ok_count": result.readiness.get("ok_count"),
            "total": result.readiness.get("total"),
            "checklist": result.readiness.get("checklist"),
        },
        "xml_sha_prefix": _short_hash(result.xml),
        "xml_length": len(result.xml.encode("utf-8")),
        "disclaimer": (
            "Export only — secrets redacted. Live submit requires presenter "
            "credentials, package reference, and CH_XML_SUBMIT_LIVE=1."
        ),
    }
    pack.xml_export_json = json.dumps(record, default=str)
    pack.xml_last_export_at = datetime.utcnow()
    pack.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pack)
    result.meta["saved"] = True
    return result


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def export_dict(pack: CsPack) -> Dict[str, Any]:
    if not pack.xml_export_json:
        return {}
    try:
        return json.loads(pack.xml_export_json)
    except json.JSONDecodeError:
        return {}


def submit_cs01_xml(
    db: Session,
    pack: CsPack,
    client: Optional[Client],
) -> Dict[str, Any]:
    """
    Live gateway submit — gated hard.

    Slice A: refuses unless CH_XML_SUBMIT_LIVE and credentials; when enabled,
    posts XML and stores a response summary. Schema must be validated with CH
    before relying on acceptances.
    """
    status = gateway_status()
    readiness = xml_export_readiness(db, pack, client)

    if not CH_XML_SUBMIT_LIVE:
        return {
            "ok": False,
            "error": (
                "Live XML submit is disabled. Export XML for review, complete "
                "filing on WebFiling, or set CH_XML_SUBMIT_LIVE=1 after presenter "
                "account + schema validation."
            ),
            "readiness": readiness,
            "gateway": status,
        }
    if not ch_xml_gateway_configured():
        return {
            "ok": False,
            "error": "Set CH_XML_PRESENTER_ID and CH_XML_PRESENTER_AUTH first.",
            "readiness": readiness,
            "gateway": status,
        }
    if not readiness.get("can_export") or not readiness.get("auth_code", True):
        # re-check submit blockers
        if readiness.get("submit_blockers"):
            return {
                "ok": False,
                "error": "Not ready to submit: " + ", ".join(readiness["submit_blockers"]),
                "readiness": readiness,
                "gateway": status,
            }

    built = build_cs01_xml(db, pack, client, include_auth_code=True)
    if not built.ok:
        return {
            "ok": False,
            "error": built.error or "XML build failed",
            "readiness": readiness,
            "gateway": status,
        }

    url = gateway_url()
    if not url:
        return {"ok": False, "error": "CH_XML_GATEWAY_URL not set", "gateway": status}

    # HTTP POST — application/xml
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    req = Request(
        url,
        data=built.xml.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/xml; charset=utf-8",
            "Accept": "application/xml, text/xml, */*",
            "User-Agent": f"{CH_XML_PRODUCT or 'Accologise'}/{CH_XML_PRODUCT_VERSION or '1.0'}",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = getattr(resp, "status", 200) or 200
            pack.xml_submission_status = "submitted"
            pack.xml_submission_ref = (built.meta or {}).get("transaction_id")
            pack.xml_submission_response = raw[:8000]
            pack.updated_at = datetime.utcnow()
            db.commit()
            return {
                "ok": True,
                "status_code": code,
                "response_preview": raw[:1200],
                "transaction_id": pack.xml_submission_ref,
                "gateway": status,
                "note": "Gateway accepted HTTP response — check body for CH accept/reject.",
            }
    except HTTPError as exc:
        try:
            err_raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_raw = str(exc)
        pack.xml_submission_status = f"http_{exc.code}"
        pack.xml_submission_response = err_raw[:8000]
        pack.updated_at = datetime.utcnow()
        db.commit()
        return {
            "ok": False,
            "status_code": exc.code,
            "error": f"HTTP {exc.code}",
            "response_preview": err_raw[:1200],
            "gateway": status,
        }
    except (URLError, TimeoutError, OSError) as exc:
        pack.xml_submission_status = "error"
        pack.xml_submission_response = str(exc)[:2000]
        pack.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "error": str(exc), "gateway": status}


# Avoid unused import warning for mask_secret in module API
__all__ = [
    "XmlBuildResult",
    "build_cs01_payload",
    "build_cs01_xml",
    "export_dict",
    "gateway_status",
    "gateway_url",
    "presenter_id_masked",
    "save_xml_export",
    "submit_cs01_xml",
    "xml_export_readiness",
    "mask_secret",
]
