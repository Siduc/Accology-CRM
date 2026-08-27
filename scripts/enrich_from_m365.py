#!/usr/bin/env python3
"""Fill EMPTY Accology CRM fields from Simon's M365 mailbox + OneDrive.

Empty-only. Never overwrite non-blank. Never store passwords.
Leave DATABASE_URL unset (local crm.db).
"""
from __future__ import annotations

import html as html_lib
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# --- DATABASE_URL must stay unset so we hit local crm.db ---
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
for _k in (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
    "SQLALCHEMY_DATABASE_URI",
):
    os.environ.pop(_k, None)
os.environ["ENV"] = "development"

# Bootstrap dotenv then strip DB URL again so postgres from .env cannot win.
sys.path.insert(0, str(ROOT))
from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()
for _k in (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
    "SQLALCHEMY_DATABASE_URI",
):
    os.environ.pop(_k, None)

from app.database import SessionLocal, DATABASE_URL, IS_SQLITE  # noqa: E402
from app.models.client import Client  # noqa: E402
from app.models.email_message import EmailMessage  # noqa: E402
from app.models.person import Person  # noqa: E402
from app.services.company_numbers import normalize_company_number  # noqa: E402
from app.services.ms_graph_drive import download_file as drive_download  # noqa: E402
from app.services.ms_graph_oauth import (  # noqa: E402
    get_valid_access_token,
    graph_get,
)

AUDIT_PATH = ROOT / "docs" / "enrichment-pass-2026-08-25.md"
LOCAL_DOC_ROOTS = [
    Path(r"C:\Users\User\OneDrive - Accology\Accologise Documents"),
    Path(r"C:\Users\User\OneDrive\Documents"),
    Path(r"C:\Users\User\Documents"),
    Path(r"C:\Users\User\OneDrive - Accology\Documents"),
]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
UA = "AccologiseCRM/1.0 (enrichment-pass)"

SEARCH_TERMS = [
    "PAYE",
    "Accounts Office",
    "Government Gateway",
    "authentication code",
    "personal code",
    "UTR",
]

INTERNAL_EMAIL_DOMAINS = {
    "accology.co",
    "accologise.co",
    "accologise.com",
    "hmrc.gov.uk",
    "tax.service.gov.uk",
    "notifications.service.gov.uk",
    "companieshouse.gov.uk",
    "companies.service.gov.uk",
    "microsoft.com",
    "email.microsoftonline.com",
}

PASSWORD_NEAR = re.compile(
    r"(?i)\b(password|passwd|\bpwd\b|passphrase|xero\s+password|"
    r"gateway\s+password|accounts?\s+software\s+password)\b"
)

PAYE_LABELED = re.compile(
    r"(?i)(?:employer\s+)?paye\s+(?:scheme\s+)?(?:reference|ref\.?)\s*[:\-]?\s*"
    r"(\d{3}\s*/\s*[A-Z]{1,3}\s*\d{1,7})"
)
PAYE_BARE = re.compile(r"\b(\d{3}/[A-Z]{1,2}\d{2,7})\b", re.I)

AO_LABELED = re.compile(
    r"(?i)accounts\s*office\s*(?:reference|ref\.?)?\s*[:\-]?\s*"
    r"(\d{3}\s*P\s*[A-Z]\s*\d{7,8})"
)
AO_BARE = re.compile(r"\b(\d{3}P[A-Z]\d{7,8})\b", re.I)

GG_LABELED = re.compile(
    r"(?i)(?:government\s+gateway|gateway)\s*"
    r"(?:user\s*(?:id|name)|id|username|user\s*id|user)?\s*[:\-]?\s*"
    r"(\d{10,12})"
)

PHONE_LABELED = re.compile(
    r"(?i)(?:(?:tel(?:ephone)?|phone|mobile|mob)\.?\s*(?:no\.?|number)?)\s*[:\-]?\s*"
    r"(\+?44[\d\s\-()]{9,16}|0\d[\d\s\-()]{8,14})"
)

EMAIL_LABELED = re.compile(
    r"(?i)(?:e-?mail)\s*(?:address)?\s*[:\-]?\s*"
    r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})"
)
EMAIL_BARE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)

CH_PERSONAL = re.compile(
    r"(?i)(?:personal\s+(?:identification\s+)?code|"
    r"companies\s*house\s+personal\s+code|"
    r"ch\s+personal\s+code)\s*[:\-]?\s*([A-Z0-9]{6,12})"
)

CN_LABELED = re.compile(
    r"(?i)(?:company(?:\s+registration)?\s+number|co(?:mpany)?\s*no\.?|"
    r"registered\s+number|crn)\s*[:\-]?\s*"
    r"((?:SC|NI|OC|SO|NC|R0|LP|SL|NL)?\d{6,8})"
)

NAME_STRIP = re.compile(r"[^\w\s&]", re.U)
WS = re.compile(r"\s+")

CLIENT_FIELDS = (
    "gov_gateway_username",
    "paye_reference",
    "accounts_office_reference",
    "phone",
    "email",
)
PERSON_FIELDS = ("ch_code", "phone", "email")
NEVER_WRITE = {
    "gov_gateway_password",
    "accounts_software_password",
    "xero_password",
}


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def is_blank(val: Any) -> bool:
    return val is None or str(val).strip() == ""


def norm_name(name: str) -> str:
    s = (name or "").replace("\xa0", " ").strip().lower()
    s = re.sub(r"\bltd\.?\b", "limited", s)
    s = re.sub(r"\bplc\.?\b", "plc", s)
    s = NAME_STRIP.sub(" ", s)
    s = WS.sub(" ", s).strip()
    return s


def html_to_text(raw: str) -> str:
    s = raw or ""
    s = re.sub(r"(?is)<script.*?>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?>.*?</style>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n", s)
    s = re.sub(r"(?i)</div>", "\n", s)
    s = re.sub(r"(?i)</tr>", "\n", s)
    s = re.sub(r"(?i)</h[1-6]>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def docx_bytes_text(data: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", " ", xml)
    return html_lib.unescape(xml)


def pdf_bytes_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages[:30]):
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception:
        return ""


def xlsx_bytes_text(data: bytes) -> str:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: List[str] = []
        for si, ws in enumerate(wb.worksheets[:8]):
            parts.append(ws.title or "")
            for ri, row in enumerate(ws.iter_rows(max_row=80, max_col=20, values_only=True)):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append(" ".join(cells))
        wb.close()
        return "\n".join(parts)
    except Exception:
        return ""


def bytes_to_text(data: bytes, name: str = "", content_type: str = "") -> str:
    if not data:
        return ""
    n = (name or "").lower()
    ct = (content_type or "").lower()
    if n.endswith(".pdf") or "pdf" in ct:
        return pdf_bytes_text(data)
    if n.endswith(".docx") or "wordprocessingml" in ct:
        return docx_bytes_text(data)
    if n.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in ct:
        return xlsx_bytes_text(data)
    if n.endswith(".doc"):
        # binary .doc — skip rather than dump garbage
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    if "<html" in text[:500].lower() or n.endswith((".html", ".htm")):
        return html_to_text(text)
    return text


def normalize_paye(raw: str) -> Optional[str]:
    s = re.sub(r"\s+", "", (raw or "").upper())
    if re.fullmatch(r"\d{3}/[A-Z]{1,3}\d{1,7}", s):
        return s
    return None


def normalize_ao(raw: str) -> Optional[str]:
    s = re.sub(r"\s+", "", (raw or "").upper())
    m = re.fullmatch(r"(\d{3}P[A-Z])(\d{7,8})", s)
    if not m:
        return None
    digits = m.group(2).zfill(8)
    return m.group(1) + digits


def normalize_gg(raw: str) -> Optional[str]:
    s = re.sub(r"\D", "", raw or "")
    if 10 <= len(s) <= 12:
        return s
    return None


def normalize_phone(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if digits.startswith("44") and len(digits) >= 12:
        digits = "0" + digits[2:]
    if len(digits) < 10 or len(digits) > 13:
        return None
    if not digits.startswith("0"):
        return None
    # collapse to a tidy UK form
    if digits.startswith("07") and len(digits) == 11:
        return f"{digits[:5]} {digits[5:8]} {digits[8:]}"
    return digits


def normalize_email(raw: str) -> Optional[str]:
    s = (raw or "").strip().lower().rstrip(".,;>")
    s = s.lstrip("<")
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", s):
        return None
    domain = s.rsplit("@", 1)[-1]
    if domain in INTERNAL_EMAIL_DOMAINS or domain.endswith(".gov.uk"):
        return None
    if s.endswith("@accology.co") or "accologise" in domain:
        return None
    return s


def normalize_ch_code(raw: str) -> Optional[str]:
    s = re.sub(r"\s+", "", (raw or "").upper())
    if re.fullmatch(r"[A-Z0-9]{6,8}", s):
        return s
    return None


def looks_like_password_context(text: str) -> bool:
    return bool(PASSWORD_NEAR.search(text or ""))


def graph_request(
    path: str,
    access_token: str,
    *,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    method: str = "GET",
    data: Optional[bytes] = None,
) -> Tuple[bool, Any, str]:
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, data=data, method=method, headers=headers)
    for attempt in range(4):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if not raw:
                    return True, {}, ""
                if "application/json" in ctype or raw[:1] in (b"{", b"["):
                    try:
                        return True, json.loads(raw.decode("utf-8")), ""
                    except json.JSONDecodeError:
                        return True, raw, ""
                return True, raw, ""
        except HTTPError as exc:
            try:
                err = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err = str(exc)
            if exc.code == 429 and attempt < 3:
                wait = 5 * (attempt + 1)
                try:
                    wait = int(exc.headers.get("Retry-After") or wait)
                except Exception:
                    pass
                time.sleep(min(wait, 30))
                continue
            return False, {}, f"HTTP {exc.code}: {err[:400]}"
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return False, {}, str(exc)
    return False, {}, "retries exhausted"


def graph_search_messages(access_token: str, term: str, limit: int = 200) -> Tuple[List[dict], str]:
    q = quote(f'"{term}"', safe="")
    url = (
        f"/me/messages?$search={q}"
        f"&$select=id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
        f"bodyPreview,hasAttachments,internetMessageId,webLink"
        f"&$top=50"
    )
    items: List[dict] = []
    seen = set()
    err_last = ""
    while url and len(items) < limit:
        ok, data, err = graph_request(
            url,
            access_token,
            extra_headers={"ConsistencyLevel": "eventual"},
            timeout=45,
        )
        if not ok:
            err_last = err
            break
        if not isinstance(data, dict):
            break
        for row in data.get("value") or []:
            mid = row.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                items.append(row)
        url = (data.get("@odata.nextLink") or "").strip() or None
        if url and not url.startswith("http") and not url.startswith("/"):
            url = None
    return items, err_last


def graph_get_message_body(access_token: str, message_id: str) -> str:
    ok, data, err = graph_request(
        f"/me/messages/{quote(message_id, safe='')}"
        f"?$select=id,subject,body,uniqueBody,from,toRecipients,hasAttachments",
        access_token,
        timeout=45,
    )
    if not ok or not isinstance(data, dict):
        return ""
    parts = []
    for key in ("uniqueBody", "body"):
        blob = data.get(key) or {}
        if isinstance(blob, dict):
            content = blob.get("content") or ""
            ctype = (blob.get("contentType") or "").lower()
            if ctype == "html" or "<" in content[:80]:
                parts.append(html_to_text(content))
            else:
                parts.append(content)
    return "\n".join(p for p in parts if p)


def graph_get_attachments_text(access_token: str, message_id: str) -> str:
    ok, data, err = graph_request(
        f"/me/messages/{quote(message_id, safe='')}/attachments"
        f"?$top=20&$select=id,name,contentType,size,isInline",
        access_token,
        timeout=45,
    )
    if not ok or not isinstance(data, dict):
        return ""
    chunks: List[str] = []
    for att in data.get("value") or []:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or ""
        size = int(att.get("size") or 0)
        ctype = att.get("contentType") or ""
        odata = att.get("@odata.type") or ""
        if size > 8_000_000:
            continue
        low = name.lower()
        useful = low.endswith(
            (".pdf", ".docx", ".txt", ".csv", ".html", ".htm", ".xlsx", ".eml")
        )
        if not useful and "fileattachment" not in odata.lower():
            continue
        ok2, full, err2 = graph_request(
            f"/me/messages/{quote(message_id, safe='')}/attachments/{quote(att.get('id') or '', safe='')}",
            access_token,
            timeout=60,
        )
        if not ok2 or not isinstance(full, dict):
            continue
        b64 = full.get("contentBytes")
        if not b64:
            continue
        import base64

        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        chunks.append(bytes_to_text(raw, name=name, content_type=ctype))
    return "\n".join(c for c in chunks if c)


def graph_search_drive(access_token: str, term: str, limit: int = 80) -> Tuple[List[dict], str]:
    q = quote(term, safe="")
    url = f"/me/drive/root/search(q='{q}')?$top=50&$select=id,name,webUrl,file,folder,parentReference,size"
    items: List[dict] = []
    seen = set()
    err_last = ""
    while url and len(items) < limit:
        ok, data, err = graph_request(url, access_token, timeout=45)
        if not ok:
            err_last = err
            break
        if not isinstance(data, dict):
            break
        for row in data.get("value") or []:
            iid = row.get("id")
            if iid and iid not in seen and row.get("file"):
                seen.add(iid)
                items.append(row)
        url = (data.get("@odata.nextLink") or "").strip() or None
    return items, err_last


class Indexes:
    def __init__(self, clients: List[Client], people: List[Person]):
        self.clients = {c.id: c for c in clients}
        self.people = {p.id: p for p in people}
        self.by_number: Dict[str, List[int]] = defaultdict(list)
        self.by_name: Dict[str, List[int]] = defaultdict(list)
        self.by_person: Dict[str, List[int]] = defaultdict(list)
        self.person_to_clients: Dict[int, List[int]] = defaultdict(list)
        for c in clients:
            cn = normalize_company_number(c.company_number or "") or ""
            if cn and not cn.startswith("IND-"):
                self.by_number[cn].append(c.id)
                digits = re.sub(r"\D", "", cn)
                if digits and digits != cn:
                    self.by_number[digits.lstrip("0") or digits].append(c.id)
            nn = norm_name(c.company_name or "")
            if nn and len(nn) >= 5:
                self.by_name[nn].append(c.id)
        for p in people:
            nn = norm_name(p.full_name or "")
            if nn and " " in nn and len(nn) >= 6:
                self.by_person[nn].append(p.id)
            for c in p.clients or []:
                self.person_to_clients[p.id].append(c.id)
            if p.client_id:
                self.person_to_clients[p.id].append(p.client_id)


def match_entity(idx: Indexes, text: str) -> Tuple[Optional[str], Optional[int], str]:
    """Return (kind, id, how) or (None, None, reason). kind is client|person."""
    blob = text or ""
    blob_u = blob.upper()
    blob_n = norm_name(blob)

    number_hits: List[int] = []
    for cn, ids in idx.by_number.items():
        if len(cn) < 6:
            continue
        if cn in blob_u or cn.lstrip("0") in blob_u:
            # word-ish boundary
            if re.search(rf"(?<![A-Z0-9]){re.escape(cn)}(?![A-Z0-9])", blob_u):
                number_hits.extend(ids)
            elif cn.isdigit() and re.search(rf"(?<!\d){re.escape(cn)}(?!\d)", blob_u):
                number_hits.extend(ids)
    number_hits = list(dict.fromkeys(number_hits))
    if len(number_hits) == 1:
        return "client", number_hits[0], "company_number"
    if len(number_hits) > 1:
        return None, None, "ambiguous_company_number"

    name_hits: List[int] = []
    for nn, ids in idx.by_name.items():
        if nn and nn in blob_n:
            name_hits.extend(ids)
    name_hits = list(dict.fromkeys(name_hits))
    if len(name_hits) == 1:
        return "client", name_hits[0], "exact_name"
    if len(name_hits) > 1:
        return None, None, "ambiguous_name"

    person_hits: List[int] = []
    for nn, ids in idx.by_person.items():
        if nn and nn in blob_n:
            person_hits.extend(ids)
    person_hits = list(dict.fromkeys(person_hits))
    if len(person_hits) == 1:
        pid = person_hits[0]
        cids = list(dict.fromkeys(idx.person_to_clients.get(pid) or []))
        if len(cids) == 1:
            return "client", cids[0], "person_single_client"
        if len(cids) == 0:
            return "person", pid, "person"
        # person linked to several companies — still a unique person
        return "person", pid, "person_multi_client"
    if len(person_hits) > 1:
        return None, None, "ambiguous_person"
    return None, None, "no_match"


def extract_fields(text: str) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = defaultdict(list)

    def add(field: str, val: Optional[str]) -> None:
        if val and val not in found[field]:
            found[field].append(val)

    for m in PAYE_LABELED.finditer(text):
        add("paye_reference", normalize_paye(m.group(1)))
    if not found["paye_reference"]:
        for m in PAYE_BARE.finditer(text):
            add("paye_reference", normalize_paye(m.group(1)))

    for m in AO_LABELED.finditer(text):
        add("accounts_office_reference", normalize_ao(m.group(1)))
    if not found["accounts_office_reference"]:
        for m in AO_BARE.finditer(text):
            add("accounts_office_reference", normalize_ao(m.group(1)))

    for m in GG_LABELED.finditer(text):
        add("gov_gateway_username", normalize_gg(m.group(1)))

    for m in CH_PERSONAL.finditer(text):
        add("ch_code", normalize_ch_code(m.group(1)))

    for m in PHONE_LABELED.finditer(text):
        add("phone", normalize_phone(m.group(1)))

    for m in EMAIL_LABELED.finditer(text):
        add("email", normalize_email(m.group(1)))

    return found


def empty_counts(db) -> Dict[str, int]:
    clients = db.query(Client).all()
    people = db.query(Person).all()
    out = {
        "clients_total": len(clients),
        "people_total": len(people),
    }
    for f in CLIENT_FIELDS:
        out[f"clients.{f}"] = sum(1 for c in clients if is_blank(getattr(c, f, None)))
    for f in PERSON_FIELDS:
        out[f"people.{f}"] = sum(1 for p in people if is_blank(getattr(p, f, None)))
    return out


def fmt_counts(d: Dict[str, int]) -> str:
    lines = []
    for k in sorted(d):
        lines.append(f"- {k}: {d[k]}")
    return "\n".join(lines)


class Audit:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.fills: List[str] = []
        self.passwords: List[str] = []
        self.skipped: List[str] = []
        self.notes: List[str] = []

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(msg, flush=True)

    def add_fill(self, entity: str, eid: int, field: str, source: str) -> None:
        self.fills.append(f"| {entity} {eid} | {field} | {source} |")

    def write(self, before: Dict[str, int], after: Dict[str, int], header_extra: str) -> None:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        body = []
        body.append("# Enrichment pass 2026-08-25")
        body.append("")
        body.append(f"Generated {now_iso()} (Europe/London BST).")
        body.append("")
        body.append("Empty-only. Never overwrite non-blank. Passwords never stored.")
        body.append("Values of codes / usernames / phones / emails are not written here.")
        body.append("")
        body.append("## Token / sources")
        body.append("")
        body.append(header_extra)
        body.append("")
        body.append("## Snapshot before")
        body.append("")
        body.append(fmt_counts(before))
        body.append("")
        body.append("## Snapshot after")
        body.append("")
        body.append(fmt_counts(after))
        body.append("")
        body.append("## Fills (id, field, source — not the value)")
        body.append("")
        if self.fills:
            body.append("| id | field | source |")
            body.append("|----|-------|--------|")
            body.extend(self.fills)
        else:
            body.append("_No fills._")
        body.append("")
        body.append("## Password seen, not stored")
        body.append("")
        if self.passwords:
            body.extend(f"- {n}" for n in self.passwords)
        else:
            body.append("_None._")
        body.append("")
        body.append("## Skipped (ambiguous / no unique match)")
        body.append("")
        if self.skipped:
            body.extend(f"- {s}" for s in self.skipped[:200])
            if len(self.skipped) > 200:
                body.append(f"- … {len(self.skipped) - 200} more")
        else:
            body.append("_None._")
        body.append("")
        body.append("## Notes")
        body.append("")
        body.extend(f"- {n}" for n in self.notes)
        body.append("")
        AUDIT_PATH.write_text("\n".join(body), encoding="utf-8")


def apply_candidate(
    db,
    idx: Indexes,
    kind: str,
    eid: int,
    fields: Dict[str, List[str]],
    source: str,
    audit: Audit,
    filled_set: set,
) -> None:
    if kind == "client":
        obj = idx.clients.get(eid)
        allowed = CLIENT_FIELDS
        label = "client"
    else:
        obj = idx.people.get(eid)
        allowed = PERSON_FIELDS
        label = "person"
    if obj is None:
        return
    name = ""
    if kind == "client":
        name = (obj.company_name or f"client #{eid}").strip()
    else:
        name = (obj.full_name or f"person #{eid}").strip()

    for field, values in fields.items():
        if field not in allowed:
            continue
        if field in NEVER_WRITE:
            continue
        uniq = [v for v in values if v]
        uniq = list(dict.fromkeys(uniq))
        if not uniq:
            continue
        if len(uniq) > 1:
            audit.skipped.append(f"{label} {eid} {field} ambiguous_values source={source}")
            continue
        val = uniq[0]
        cur = getattr(obj, field, None)
        if not is_blank(cur):
            continue
        key = (label, eid, field)
        if key in filled_set:
            continue
        setattr(obj, field, val)
        if kind == "client":
            obj.updated_at = datetime.utcnow()
        db.add(obj)
        db.commit()
        filled_set.add(key)
        audit.add_fill(label, eid, field, source)
        audit.note(f"filled {label} {eid} {field} from {source}")

    # person.ch_code only lives on people; if we matched a client, try linked unique person
    if kind == "client" and fields.get("ch_code"):
        people = [p for p in (obj.people or [])]
        if len(people) == 1:
            apply_candidate(
                db,
                idx,
                "person",
                people[0].id,
                {"ch_code": fields["ch_code"]},
                source,
                audit,
                filled_set,
            )
        elif len(people) > 1:
            audit.skipped.append(f"client {eid} ch_code skipped (multiple people) source={source}")


def harvest_text(
    db,
    idx: Indexes,
    text: str,
    source: str,
    audit: Audit,
    filled_set: set,
    password_logged: set,
) -> None:
    if not text or len(text.strip()) < 8:
        return
    kind, eid, how = match_entity(idx, text)
    pwd = looks_like_password_context(text)
    if pwd:
        cname = "unmatched"
        if kind == "client" and eid in idx.clients:
            cname = (idx.clients[eid].company_name or f"client #{eid}").strip()
        elif kind == "person" and eid in idx.people:
            cname = (idx.people[eid].full_name or f"person #{eid}").strip()
            cids = idx.person_to_clients.get(eid) or []
            if len(cids) == 1 and cids[0] in idx.clients:
                cname = (idx.clients[cids[0]].company_name or cname).strip()
        if cname not in password_logged:
            password_logged.add(cname)
            audit.passwords.append(f"password seen, not stored — {cname}")
            audit.note(f"password seen, not stored — {cname}")
    if kind is None or eid is None:
        return
    fields = extract_fields(text)
    if not fields:
        return
    apply_candidate(db, idx, kind, eid, fields, f"{source} ({how})", audit, filled_set)
    # If we matched a unique person with a single client, also apply client fields
    if kind == "person":
        cids = list(dict.fromkeys(idx.person_to_clients.get(eid) or []))
        if len(cids) == 1:
            apply_candidate(
                db, idx, "client", cids[0], fields, f"{source} ({how})", audit, filled_set
            )


def file_name_interesting(name: str) -> bool:
    n = (name or "").lower()
    keys = (
        "paye",
        "gateway",
        "accounts office",
        "accountsoffice",
        "ao ref",
        "utr",
        "authentication",
        "personal code",
        "onboard",
        "engagement",
        "kyc",
        "hmrc",
        "employer",
        "payroll",
        "p30",
        "epaye",
        "gov.uk",
    )
    return any(k in n for k in keys)


def scan_local_files(
    db, idx: Indexes, audit: Audit, filled_set: set, password_logged: set
) -> int:
    scanned = 0
    roots = [p for p in LOCAL_DOC_ROOTS if p.exists()]
    audit.note("local roots: " + (", ".join(str(r) for r in roots) or "(none found)"))
    exts = {".pdf", ".docx", ".txt", ".csv", ".md", ".html", ".htm", ".xlsx", ".eml"}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # skip huge noise dirs
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() not in {".git", "node_modules", "__pycache__", ".svn"}
            ]
            parent = Path(dirpath).name.lower()
            parent_interesting = parent in {
                "correspondence",
                "id-kyc",
                "engagement letter",
                "tax return",
                "tax",
                "payroll",
                "kyc",
            }
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() not in exts:
                    continue
                if not (file_name_interesting(fn) or parent_interesting):
                    continue
                try:
                    if p.stat().st_size > 8_000_000:
                        continue
                    data = p.read_bytes()
                except Exception:
                    continue
                text = bytes_to_text(data, name=fn)
                if not text:
                    continue
                scanned += 1
                rel = str(p)
                harvest_text(db, idx, f"{fn}\n{text}", f"local:{fn}", audit, filled_set, password_logged)
                if scanned % 25 == 0:
                    audit.note(f"local files scanned {scanned}")
    return scanned


def main() -> int:
    audit = Audit()
    print("=== Accology CRM enrichment 2026-08-25 ===", flush=True)
    print(f"cwd={ROOT}", flush=True)

    if not IS_SQLITE:
        print("REFUSING: database is not sqlite. DATABASE_URL must stay unset.", flush=True)
        return 2
    if "crm.db" not in str(DATABASE_URL):
        print(f"REFUSING: unexpected sqlite url host={DATABASE_URL[:40]}...", flush=True)
        return 2

    db = SessionLocal()
    filled_set: set = set()
    password_logged: set = set()
    token_notes = []
    try:
        before = empty_counts(db)
        audit.note("snapshot before taken")
        for k, v in before.items():
            audit.note(f"  {k}={v}")

        clients = db.query(Client).all()
        people = db.query(Person).all()
        idx = Indexes(clients, people)
        audit.note(f"indexed clients={len(clients)} people={len(people)}")

        token, terr = get_valid_access_token(db)
        graph_ok = bool(token)
        if not token:
            token_notes.append(f"Graph token FAILED: {terr or 'unknown'}")
            audit.note(f"Graph token failed: {terr}")
        else:
            ok, me, err = graph_get("/me", token)
            if ok:
                email = me.get("mail") or me.get("userPrincipalName") or ""
                token_notes.append(f"Graph token OK. mailbox={email}")
                audit.note(f"Graph token OK mailbox={email}")
            else:
                token_notes.append(f"Graph /me failed: {err}")
                audit.note(f"Graph /me failed: {err}")
                token = None
                graph_ok = False

        mail_count = 0
        drive_count = 0
        if token:
            seen_msg = set()
            for term in SEARCH_TERMS:
                rows, err = graph_search_messages(token, term)
                audit.note(f"mail search '{term}' hits={len(rows)} err={err or '-'}")
                if err:
                    token_notes.append(f"mail search '{term}': {err}")
                for row in rows:
                    mid = row.get("id")
                    if not mid or mid in seen_msg:
                        continue
                    seen_msg.add(mid)
                    subj = row.get("subject") or ""
                    preview = row.get("bodyPreview") or ""
                    frm = ""
                    try:
                        frm = (
                            ((row.get("from") or {}).get("emailAddress") or {}).get("address")
                            or ""
                        )
                    except Exception:
                        frm = ""
                    body = graph_get_message_body(token, mid)
                    att = ""
                    if row.get("hasAttachments"):
                        att = graph_get_attachments_text(token, mid)
                    blob = "\n".join(
                        [
                            subj,
                            frm,
                            preview,
                            body,
                            att,
                        ]
                    )
                    src = f"mail:{subj[:80] or '(no subject)'}"
                    harvest_text(db, idx, blob, src, audit, filled_set, password_logged)
                    mail_count += 1
                    if mail_count % 20 == 0:
                        audit.note(f"mail messages processed {mail_count}")
                    # refresh token occasionally
                    if mail_count % 80 == 0:
                        token, terr = get_valid_access_token(db)
                        if not token:
                            audit.note(f"token refresh failed mid-mail: {terr}")
                            break
                token, _ = get_valid_access_token(db)
                if not token:
                    break

            token, terr = get_valid_access_token(db)
            if token:
                seen_files = set()
                for term in SEARCH_TERMS:
                    rows, err = graph_search_drive(token, term)
                    audit.note(f"onedrive search '{term}' files={len(rows)} err={err or '-'}")
                    if err:
                        token_notes.append(f"onedrive search '{term}': {err}")
                    for row in rows:
                        iid = row.get("id")
                        if not iid or iid in seen_files:
                            continue
                        size = int(row.get("size") or 0)
                        name = row.get("name") or ""
                        if size > 8_000_000:
                            continue
                        seen_files.add(iid)
                        data, ctype, derr = drive_download(token, iid)
                        if not data:
                            continue
                        text = bytes_to_text(data, name=name, content_type=ctype)
                        harvest_text(
                            db,
                            idx,
                            f"{name}\n{text}",
                            f"onedrive:{name}",
                            audit,
                            filled_set,
                            password_logged,
                        )
                        drive_count += 1
                        if drive_count % 15 == 0:
                            audit.note(f"onedrive files processed {drive_count}")
            else:
                token_notes.append(f"token lost before OneDrive: {terr}")

        else:
            token_notes.append("Falling back to email_messages + local OneDrive files.")
            audit.note("Graph unavailable — fallback to email_messages + local files")

        # Always also scan local files + CRM email_messages (cheap extra recall)
        local_n = scan_local_files(db, idx, audit, filled_set, password_logged)
        audit.note(f"local files harvested={local_n}")

        em_n = 0
        for em in db.query(EmailMessage).all():
            blob = "\n".join(
                [
                    em.subject or "",
                    em.to_address or "",
                    em.cc_address or "",
                    em.body or "",
                ]
            )
            src = f"email_messages#{em.id}"
            # If already linked to a client, still harvest but matching remains strict
            harvest_text(db, idx, blob, src, audit, filled_set, password_logged)
            em_n += 1
        audit.note(f"email_messages scanned={em_n}")

        # expire cached objects then recount
        db.expire_all()
        after = empty_counts(db)
        audit.note("snapshot after taken")
        for k, v in after.items():
            audit.note(f"  {k}={v}")

        header = "\n".join(
            [
                *(f"- {t}" for t in token_notes),
                f"- mail messages processed: {mail_count}",
                f"- onedrive files processed: {drive_count}",
                f"- local files harvested: {local_n}",
                f"- email_messages scanned: {em_n}",
                f"- fills applied: {len(audit.fills)}",
                f"- password-seen logs: {len(audit.passwords)}",
                "- DATABASE_URL left unset; target is local crm.db",
            ]
        )
        audit.write(before, after, header)
        audit.note(f"audit written {AUDIT_PATH}")
        print(f"DONE fills={len(audit.fills)} passwords_seen={len(audit.passwords)}", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
