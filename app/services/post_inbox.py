"""
Post inbox — import scanner drops, split PDFs, classify, review actions, learn rules.

Typical layout (created automatically):
  {POST_INBOX_PATH}/
    inbox/       ← scanner writes here
    processing/
    done/
    failed/
    splits/      ← per-item PDFs for preview / filing
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.client import Client
from app.models.document import DOCUMENT_CATEGORIES
from app.models.job import Job
from app.models.post_inbox import (
    POST_ACTIONS,
    POST_CATEGORIES,
    PostBatch,
    PostItem,
    PostRule,
    PostSplitCue,
    PostSplitLesson,
)
from app.services.client_matching import match_clients_ranked, normalize_client_name
from app.services.company_numbers import normalize_company_number

logger = logging.getLogger("accountant_crm.post_inbox")

# HMRC / standard government correspondence cues
_HMRC_KEYWORDS = (
    "hmrc",
    "hm revenue",
    "hm revenue & customs",
    "self assessment",
    "corporation tax",
    "paye",
    "vat return",
    "ct600",
    "sa302",
    "tax code",
    "national insurance",
    "penalties and surcharges",
    "coding notice",
)
_CHASE_KEYWORDS = (
    "overdue",
    "final demand",
    "final notice",
    "reminder",
    "outstanding balance",
    "payment due",
    "chase",
    "late payment",
    "collection",
    "statutory demand",
    "letter before action",
)
_CH_KEYWORDS = (
    "companies house",
    "confirmation statement",
    "accounts filing",
    "company number",
)


def post_inbox_root() -> Path:
    from app import config

    raw = (getattr(config, "POST_INBOX_PATH", None) or "").strip()
    if not raw:
        from app.config import _default_post_inbox_path

        raw = _default_post_inbox_path()
    return Path(raw)


def count_pending_scan_files() -> int:
    """How many scan files are waiting to be imported (inbox + root)."""
    dirs = ensure_inbox_dirs()
    n = 0
    for folder in (dirs["inbox"], dirs["root"]):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if not p.is_file():
                continue
            if p.parent == dirs["root"] and p.name.lower() in {
                "desktop.ini",
                "thumbs.db",
            }:
                continue
            if p.suffix.lower() in {
                ".pdf",
                ".png",
                ".jpg",
                ".jpeg",
                ".tif",
                ".tiff",
                ".webp",
            }:
                n += 1
    return n


def ensure_item_file(db: Session, item: PostItem) -> bool:
    """
    Ensure the per-item PDF exists on disk for preview.
    Regenerates from the batch archive when splits/ was cleared.
    """
    if item.local_path and Path(item.local_path).is_file():
        return True
    batch = item.batch or (
        db.query(PostBatch).filter(PostBatch.id == item.batch_id).first()
    )
    if not batch:
        return False
    src = None
    for cand in (batch.archived_path, batch.source_path):
        if cand and Path(cand).is_file():
            src = Path(cand)
            break
    if not src:
        return False
    dirs = ensure_inbox_dirs()
    ps = int(item.page_start or 1)
    pe = int(item.page_end or ps)
    out = dirs["splits"] / f"batch{batch.id}_p{ps}-{pe}.pdf"
    ok, err = _split_pdf_pages(src, out, ps, pe)
    if not ok:
        # whole-file fallback for non-PDF or bad range
        if src.suffix.lower() == ".pdf":
            logger.warning("Regenerate split failed for item %s: %s", item.id, err)
            return False
        out = dirs["splits"] / f"batch{batch.id}_full{src.suffix.lower()}"
        try:
            shutil.copy2(str(src), str(out))
        except Exception:
            return False
    item.local_path = str(out)
    item.size_bytes = int(out.stat().st_size) if out.is_file() else 0
    try:
        db.commit()
    except Exception:
        db.rollback()
    return out.is_file()


def ensure_inbox_dirs() -> Dict[str, Path]:
    root = post_inbox_root()
    dirs = {
        "root": root,
        "inbox": root / "inbox",
        "processing": root / "processing",
        "done": root / "done",
        "failed": root / "failed",
        "splits": root / "splits",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _text_score(t: str) -> int:
    """Prefer cleaner extract (Ahmed Bros invoice pipeline)."""
    if not t:
        return -10_000_000
    score = len(t)
    words = re.findall(r"[A-Za-z]{3,}", t)
    if words:
        weird = sum(1 for w in words if not re.search(r"[aeiouAEIOU]", w))
        if weird / max(len(words), 1) > 0.35:
            score -= 5000
    return score


def extract_pdf_page_texts(path: Path) -> Tuple[List[str], str]:
    """
    Per-page text extraction (pdfplumber preferred, pypdf fallback).

    Same approach as Ahmed Bros invoice day-book pipeline.
    Returns (list of page texts 0-indexed, error_or_empty).
    """
    path = Path(path)
    pages_pl: List[str] = []
    pages_py: List[str] = []
    err = ""

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as doc:
            for pg in doc.pages:
                try:
                    pages_pl.append(pg.extract_text() or "")
                except Exception:
                    pages_pl.append("")
    except Exception as e:
        err = f"pdfplumber: {e}"

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        for pg in reader.pages:
            try:
                pages_py.append(pg.extract_text() or "")
            except Exception:
                pages_py.append("")
    except Exception as e:
        err = (err + "; " if err else "") + f"pypdf: {e}"

    if not pages_pl and not pages_py:
        return [], err or "Failed to read PDF"

    # Align lengths; pick better text per page
    n = max(len(pages_pl), len(pages_py))
    out: List[str] = []
    for i in range(n):
        a = pages_pl[i] if i < len(pages_pl) else ""
        b = pages_py[i] if i < len(pages_py) else ""
        out.append(a if _text_score(a) >= _text_score(b) else b)
    return out, ""


def _extract_pdf_text_and_pages(path: Path) -> Tuple[int, str]:
    """Return (page_count, combined text excerpt)."""
    pages, _err = extract_pdf_page_texts(path)
    if not pages:
        # last resort single-shot pypdf
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path), strict=False)
            pages = [(pg.extract_text() or "") for pg in reader.pages]
        except Exception as exc:
            logger.warning("PDF read failed %s: %s", path, exc)
            return 0, ""
    combined = "\n".join(pages)
    return len(pages), combined[:20000]


# Patterns that often start a *new* piece of post (page-level)
_NEW_DOC_MARKERS = (
    r"\bHM\s*Revenue\s*(?:&|and)\s*Customs\b",
    r"\bHMRC\b",
    r"\bCompanies\s+House\b",
    r"\bStatutory\s+notice\b",
    r"\bThe\s+Pensions\s+Regulator\b",
    r"\bautomatic\s+enrolment\b",
    r"\bSelf\s+Assessment\b",
    r"\bCorporation\s+Tax\b",
    r"\bPAYE\b",
    r"\bVAT\s+(?:Return|Notice|Statement)\b",
    r"\bForm\s+(?:SA\d+|CT\d+|P\d+|VAT\d+|SL\d+)\b",
    r"\bDear\s+\S+",
    r"\bFinal\s+(?:demand|notice|reminder|letter)\b",
    r"\bLetter\s+before\s+action\b",
    r"\bStatutory\s+demand\b",
    r"\bInvoice\s+(?:No\.?|Number|#)\b",
    r"\bTax\s+Invoice\b",
    r"\bStatement\s+of\s+Account\b",
    r"\bReminder\s+notice\b",
    r"\bNotice\s+to\s+start\b",
    r"\bAttendance\s+Requested\b",
    r"\bIt's\s+time\s+to\s+(?:pay|resolve)\b",
    r"\bMissed\s+Mortgage\s+Payment\b",
)


def extract_page_ink_ratios(path: Path) -> List[float]:
    """Fraction of non-white pixels per page (0–1)."""
    profiles = extract_page_visual_profiles(path)
    return [p.get("ink", 0.5) for p in profiles]


def extract_page_visual_profiles(path: Path) -> List[Dict[str, Any]]:
    """
    Per-page ink + fixed-size greyscale fingerprints (full page + top letterhead band).
    Used to find blank separators and letterhead changes on image-only scans.
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return []
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    def _grid(samples: bytes, w: int, h: int, size: int, y0: int = 0, y1: Optional[int] = None) -> List[float]:
        y1 = h if y1 is None else min(h, y1)
        y0 = max(0, y0)
        out: List[float] = []
        for gy in range(size):
            for gx in range(size):
                xa = int(gx * w / size)
                xb = max(xa + 1, int((gx + 1) * w / size))
                ya = y0 + int(gy * (y1 - y0) / size)
                yb = y0 + max(1, int((gy + 1) * (y1 - y0) / size))
                total = 0
                n = 0
                for y in range(ya, min(yb, y1)):
                    row = y * w
                    for x in range(xa, min(xb, w)):
                        total += samples[row + x]
                        n += 1
                out.append(total / max(n, 1))
        return out

    try:
        doc = fitz.open(str(path))
        profiles: List[Dict[str, Any]] = []
        mat = fitz.Matrix(0.16, 0.16)
        for i in range(doc.page_count):
            try:
                pix = doc[i].get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
                samples = pix.samples or b""
                w, h = pix.width, pix.height
                if not samples or w < 2 or h < 2:
                    profiles.append({"ink": 0.0, "full": [], "top": []})
                    continue
                nonwhite = sum(1 for b in samples if b < 245)
                ink = nonwhite / max(len(samples), 1)
                full = _grid(samples, w, h, 16)
                top = _grid(samples, w, h, 12, y0=0, y1=max(1, h // 4))
                profiles.append({"ink": ink, "full": full, "top": top})
            except Exception:
                profiles.append({"ink": 0.5, "full": [], "top": []})
        doc.close()
        return profiles
    except Exception as exc:
        logger.warning("visual profiles failed for %s: %s", path, exc)
        return []


def _visual_sim(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    mad = sum(abs(x - y) for x, y in zip(a, b)) / len(a) / 255.0
    return 1.0 - mad


def _page_is_blank(text: str, ink: Optional[float] = None) -> bool:
    """
    Blank page = almost no text, and either no ink data or very little ink
    (white separator sheet between letters on multi-doc scanner jobs).

    Court packs often include *intentional* blank reverse pages — those are
    handled separately (we do not treat every blank as a document break).
    """
    s = re.sub(r"\s+", "", text or "")
    text_empty = len(s) < 8
    if ink is not None:
        # Image-only letter pages have ink but no OCR text
        if ink < 0.12:
            return True
        if text_empty and ink >= 0.12:
            return False
    return text_empty


# Court / tribunal packs often leave blank reverse sides or blank form pages
_COURT_LEGAL_KEYWORDS = (
    "county court",
    "high court",
    "crown court",
    "family court",
    "tribunal",
    "claim form",
    "particulars of claim",
    "statement of case",
    "witness statement",
    "affidavit",
    "claimant",
    "defendant",
    "applicant",
    "respondent",
    "solicitor",
    "barrister",
    "court seal",
    "sealed by",
    "civil procedure",
    "cpr ",
    "n1 ",
    "n244",
    "n460",
    "statement of truth",
    "in the matter of",
    "between:",
    "claim number",
    "case number",
    "court fee",
    "judgement",
    "judgment",
    "injunction",
    "possession order",
    "winding up",
    "petition",
)


def _combined_page_text(page_texts: List[str], limit: int = 12000) -> str:
    return "\n".join(page_texts or "")[:limit]


def _page_looks_like_court(text: str) -> bool:
    """True if this single page is court/tribunal paperwork (not a passing mention)."""
    low = (text or "").lower()
    if not low.strip():
        return False
    strong = (
        "particulars of claim",
        "statement of truth",
        "claim form",
        "witness statement",
        "county court",
        "high court",
        "magistrates court",
        "magistrates' court",
    )
    if any(k in low for k in strong):
        return True
    hits = sum(1 for k in _COURT_LEGAL_KEYWORDS if k in low)
    return hits >= 3


def _looks_like_court_or_legal_pack(page_texts: List[str]) -> bool:
    """
    True when the *whole file* is a court/tribunal pack.

    A mixed scanner pile (12 different letters) often mentions 'tribunal' or
    'county court' on one page — that must not glue the entire stack together.
    Require court language on most pages, or on a small file that is clearly court.
    """
    pages = [t for t in (page_texts or []) if (t or "").strip()]
    if not pages:
        return False
    court_pages = sum(1 for t in pages if _page_looks_like_court(t))
    n = len(pages)
    if n <= 2:
        return court_pages == n and _page_looks_like_court(pages[0])
    # Majority of pages, and at least 3, look like court paperwork
    return court_pages >= 3 and court_pages >= int(n * 0.6)


def _continuous_page_of_total(page_texts: List[str]) -> bool:
    """
    True if several pages share the same 'Page X of N' total (one multipage pack).
    """
    totals: Dict[int, int] = {}
    for t in page_texts or []:
        for m in re.finditer(
            r"\bPage\s*(\d+)\s*(?:of|/)\s*(\d+)\b", t or "", re.I
        ):
            tot = int(m.group(2))
            if tot >= 3:
                totals[tot] = totals.get(tot, 0) + 1
    if not totals:
        return False
    best_tot, best_n = max(totals.items(), key=lambda x: x[1])
    return best_n >= 2 and best_tot >= 3


def _strong_new_letter_start(text: str) -> bool:
    """Clear 'this is a different letter' cues — not weak form noise."""
    head = (text or "")[:900]
    if not head.strip() or head.strip().startswith("[scanned page"):
        return False
    if re.search(
        r"\bDear\s+\S+"
        r"|\bFinal\s+(?:demand|notice|reminder|letter)\b"
        r"|\bInvoice\s+(?:No\.?|Number)\b"
        r"|\bForm\s+(?:SA\d+|CT\d+|P\d+|SL\d+)\b"
        r"|\bHM\s*Revenue\s*(?:&|and)\s*Customs\b"
        r"|\bHMRC\b"
        r"|\bCompanies\s+House\b"
        r"|\bStatutory\s+notice\b"
        r"|\bThe\s+Pensions\s+Regulator\b"
        r"|\bNotice\s+to\s+start\b"
        r"|\bAttendance\s+Requested\b"
        r"|\bIt's\s+time\s+to\s+(?:pay|resolve)\b"
        r"|\bMissed\s+Mortgage\s+Payment\b"
        r"|\bThis\s+is\s+a\s+final\s+reminder\b",
        head,
        re.I,
    ):
        return True
    if re.search(r"\bPage\s*1\s*(?:of|/)\s*\d+\b", head, re.I) and re.search(
        r"\bDear\s+|\bInvoice\b|\bHMRC\b|\bHM\s*Revenue\b|\bAldermore\b"
        r"|\bCompanies\s+House\b|\bRegulator\b",
        head,
        re.I,
    ):
        return True
    return False


def _sender_key(text: str) -> str:
    """Coarse sender / brand identity from the top of a page."""
    head = (text or "")[:480].lower()
    if not head.strip() or head.lstrip().startswith("[scanned page"):
        return ""
    brands = (
        ("pensions regulator", "pensions-regulator"),
        ("companies house", "companies-house"),
        ("employment law advice", "elab"),
        ("employers compliance", "elab"),
        ("bpo collections", "bpo"),
        ("city of york", "york"),
        ("york council", "york"),
        ("advantis", "advantis"),
        ("aldermore", "aldermore"),
        ("lcsdr", "lcs"),
        ("1st locate", "lcs"),
    )
    for needle, key in brands:
        if needle in head:
            return key
    if re.search(r"\blcs\b", head[:180]):
        return "lcs"
    # HMRC only as the letterhead, not a collector chasing an HMRC bill
    if re.search(r"\bhm\s*revenue|\bhmrc\b", head[:220]):
        return "hmrc"
    m = re.search(r"\bdear\s+([a-z0-9][a-z0-9&.'/-]{1,48})", head)
    if m:
        return re.sub(r"[^a-z0-9]+", "", m.group(1))[:28]
    return ""


_PRACTICE_ADDRESSEE = (
    r"\baccology\s+pays\s+(?:limited|ltd)\b",
    r"\baccology\s+(?:limited|ltd)\b",
)


def _is_practice_mail(text: str) -> bool:
    """True when the letter is addressed to the practice, not a client c/o Accology."""
    head = (text or "")[:1100]
    if not head.strip():
        return False
    low = head.lower()
    if re.search(r"\bdear\s+accology\b", low):
        return True
    if re.search(r"\baccology\s+pays\s+(?:limited|ltd)\b", low):
        return True
    # First address block line is Accology Limited (not 'c/o Accology')
    if re.search(r"(?m)^\s*accology\s+(?:limited|ltd)\b", low):
        return True
    # Avoid matching 'c/o Accology' client post as practice
    if "c/o accology" in low or "c/o accology" in low.replace(" ", ""):
        return False
    if re.search(r"\baccology\s+(?:limited|ltd)\b", low) and re.search(
        r"\bdear\s+accology\b|addressed to accology|accology limited[\s,]",
        low,
    ):
        return True
    return False


def find_practice_client(db: Session, text: str = "") -> Optional[Client]:
    """Accology Pays if the letter names it, otherwise Accology Limited."""
    low = (text or "").lower()
    names = []
    if "accology pays" in low:
        names.append("Accology Pays Limited")
    names.extend(["Accology Limited", "Accology"])
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ranked = match_clients_ranked(db, name, limit=1)
        if ranked and ranked[0][1] >= 70:
            return ranked[0][0]
    return None


def _header_phrase(text: str) -> str:
    """First distinctive line — logo / title, not the address."""
    for ln in (text or "").splitlines():
        s = ln.strip()
        if len(s) < 4 or len(s) > 80:
            continue
        if s.startswith("[scanned"):
            continue
        if re.match(r"^(dear|page\s+\d)\b", s, re.I):
            continue
        if re.match(r"^\d{1,2}\s+\w+\s+20\d{2}$", s):
            continue
        return s[:80]
    return ""


def _top_sig(profile: Optional[Dict[str, Any]]) -> str:
    top = (profile or {}).get("top") or []
    if len(top) < 8:
        return ""
    # Quantise so similar letterheads hash the same
    return ",".join(str(min(15, int(v / 16))) for v in top[:48])


def _sig_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    pa = [int(x) for x in a.split(",") if x.strip().isdigit()]
    pb = [int(x) for x in b.split(",") if x.strip().isdigit()]
    n = min(len(pa), len(pb))
    if n < 8:
        return 0.0
    mad = sum(abs(pa[i] - pb[i]) for i in range(n)) / n / 15.0
    return 1.0 - mad


def _phrase_sim(a: str, b: str) -> float:
    na = re.sub(r"[^a-z0-9]+", " ", (a or "").lower()).strip()
    nb = re.sub(r"[^a-z0-9]+", " ", (b or "").lower()).strip()
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    wa, wb = set(na.split()), set(nb.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa | wb), 1)


def _looks_like_continuation_page(text: str) -> bool:
    head = (text or "")[:500].lower()
    return bool(
        re.search(
            r"\bpage\s*[2-9]\s*(?:of|/)\s*\d+\b"
            r"|\bways to (?:pay|manage)"
            r"|\bforum details\b"
            r"|\bwho (?:are we|else can help)\b"
            r"|\bappendix\b"
            r"|\bcontinued\b"
            r"|\bsee the reverse\b",
            head,
            re.I,
        )
    )


def list_split_cues(db: Session) -> List[PostSplitCue]:
    try:
        return db.query(PostSplitCue).order_by(PostSplitCue.hit_count.desc()).all()
    except Exception:
        return []


def _match_start_cue(
    text: str,
    profile: Optional[Dict[str, Any]],
    cues: List[PostSplitCue],
) -> Optional[PostSplitCue]:
    starts = [c for c in cues if (c.kind or "") == "start"]
    if not starts:
        return None
    key = _sender_key(text)
    phrase = _header_phrase(text)
    sig = _top_sig(profile)
    best: Optional[Tuple[float, PostSplitCue]] = None
    for cue in starts:
        score = 0.0
        if key and cue.sender_key and key == cue.sender_key:
            score += 0.4
        if phrase and cue.header_phrase and _phrase_sim(phrase, cue.header_phrase) >= 0.72:
            score += 0.45
        if sig and cue.top_sig and _sig_sim(sig, cue.top_sig) >= 0.84:
            score += 0.4
        if score >= 0.44 and (best is None or score > best[0]):
            best = (score, cue)
    return best[1] if best else None


def _upsert_split_cue(
    db: Session,
    *,
    kind: str,
    sender_key: str,
    header_phrase: str,
    top_sig: str,
) -> None:
    sender_key = (sender_key or "").strip()[:80]
    header_phrase = (header_phrase or "").strip()[:80]
    top_sig = (top_sig or "").strip()[:400]
    if not sender_key and not header_phrase and not top_sig:
        return
    q = db.query(PostSplitCue).filter(PostSplitCue.kind == kind)
    if sender_key:
        q = q.filter(PostSplitCue.sender_key == sender_key)
    if header_phrase:
        q = q.filter(PostSplitCue.header_phrase == header_phrase)
    row = q.first()
    if row:
        row.hit_count = int(row.hit_count or 0) + 1
        if top_sig and not row.top_sig:
            row.top_sig = top_sig
        row.updated_at = datetime.utcnow()
        return
    db.add(
        PostSplitCue(
            kind=kind,
            sender_key=sender_key or None,
            header_phrase=header_phrase or None,
            top_sig=top_sig or None,
            hit_count=1,
            notes="Taught from a human letter cut",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )


def learn_split_cues_from_items(db: Session, batch_id: int) -> int:
    """
    Remember what started each letter: logo / typeface / sender — not yesterday's
    page counts. Next pile uses these to decide where to break.
    """
    items = (
        db.query(PostItem)
        .filter(
            PostItem.batch_id == batch_id,
            PostItem.status != "holding",
        )
        .order_by(PostItem.page_start.asc())
        .all()
    )
    if len(items) < 2:
        return 0
    batch = db.query(PostBatch).filter(PostBatch.id == batch_id).first()
    profiles: List[Dict[str, Any]] = []
    if batch:
        for cand in (batch.archived_path, batch.source_path):
            if cand and Path(cand).is_file():
                profiles = extract_page_visual_profiles(Path(cand))
                break
    n = 0
    for it in items:
        text = it.text_excerpt or ""
        ps = int(it.page_start or 1)
        prof = profiles[ps - 1] if 0 <= ps - 1 < len(profiles) else {}
        _upsert_split_cue(
            db,
            kind="start",
            sender_key=_sender_key(text),
            header_phrase=_header_phrase(text),
            top_sig=_top_sig(prof),
        )
        n += 1
        pe = int(it.page_end or ps)
        if pe > ps and 0 <= pe - 1 < len(profiles):
            _upsert_split_cue(
                db,
                kind="continue",
                sender_key=_sender_key(text),
                header_phrase="continuation",
                top_sig=_top_sig(profiles[pe - 1]),
            )
    try:
        db.commit()
    except Exception:
        db.rollback()
        return 0
    return n


def _almost_same_opening(a: str, b: str) -> bool:
    """True when two pages look like copies of the same 1-page letter/form."""
    na = re.sub(r"\s+", " ", (a or "")[:500].lower()).strip()
    nb = re.sub(r"\s+", " ", (b or "")[:500].lower()).strip()
    if len(na) < 80 or len(nb) < 80:
        return False
    if na[:160] == nb[:160]:
        return True
    wa, wb = set(re.findall(r"[a-z0-9]{4,}", na)), set(re.findall(r"[a-z0-9]{4,}", nb))
    if not wa or not wb:
        return False
    return len(wa & wb) / max(len(wa | wb), 1) >= 0.82


def _normalize_page_texts_with_ink(
    page_texts: List[str], ink_ratios: List[float]
) -> List[str]:
    """
    For image-only scans, inject a placeholder on pages that have ink so
    detect_document_page_ranges does not treat the whole file as blank.
    """
    if not page_texts:
        return page_texts
    out: List[str] = []
    for i, t in enumerate(page_texts):
        ink = ink_ratios[i] if i < len(ink_ratios) else None
        if _page_is_blank(t, ink):
            out.append("")
        elif (t or "").strip():
            out.append(t)
        else:
            # Scanned content without OCR — keep a marker for split logic
            out.append(f"[scanned page {i + 1}]")
    return out


def _page_looks_like_new_document(text: str, *, is_first: bool) -> bool:
    """True if this page likely starts a new letter/document."""
    if is_first:
        return True
    if _page_is_blank(text):
        return False
    head = (text or "")[:800]
    # Strong markers near top of page
    for pat in _NEW_DOC_MARKERS:
        if re.search(pat, head, re.I):
            return True
    # "Page 1 of N" often restarts a multi-page letter
    if re.search(r"\bPage\s*1\s*(?:of|/)\s*\d+\b", head, re.I):
        return True
    return False


def detect_document_page_ranges(
    page_texts: List[str],
    ink_ratios: Optional[List[float]] = None,
    visual_profiles: Optional[List[Dict[str, Any]]] = None,
    split_cues: Optional[List[PostSplitCue]] = None,
) -> List[Tuple[int, int, str]]:
    """
    Split a multi-page PDF into documents.

    Returns list of (page_start, page_end, reason) 1-based inclusive.

    Blank pages are **kept inside** a document (court packs leave blanks).
    A blank only starts a *new* document when the following page is a strong
    new letter (Dear… / HMRC / invoice / Form SA…), not merely non-blank.

    Court/legal packs and continuous "Page X of N" packs stay as one file.
    """
    n = len(page_texts)
    if n == 0:
        return []
    if n == 1:
        return [(1, 1, "single page")]

    profiles = visual_profiles or []
    ink = ink_ratios or [p.get("ink", 0.5) for p in profiles]
    cues = split_cues or []

    def blank(i: int) -> bool:
        t = page_texts[i] if i < len(page_texts) else ""
        ik = ink[i] if i < len(ink) else None
        return _page_is_blank(t, ik)

    # Keep-together modes (court blanks, multipage packs)
    court_pack = _looks_like_court_or_legal_pack(page_texts)
    continuous = _continuous_page_of_total(page_texts)
    if court_pack or continuous:
        reason = (
            "court/legal pack — blanks kept"
            if court_pack
            else "continuous page X of N — kept together"
        )
        return [(1, n, reason)]

    # Build starts: page indices (0-based) that begin a document
    starts: List[int] = [0]
    reasons: Dict[int, str] = {0: "start of file"}

    for i in range(1, n):
        t = page_texts[i] or ""
        prev = page_texts[i - 1] or ""

        # Blank pages stay in the current document (never open a new start here)
        if blank(i):
            continue

        # After blank: only split if this page is clearly a *new letter*
        # (court reverse blanks must NOT split)
        if blank(i - 1) and not blank(i):
            if _strong_new_letter_start(t):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "new letter after blank separator"
            # else: intentional blank inside same pack — keep together
            continue

        # Learned letterhead / logo: this page looks like a known letter start
        # and is not a continuation of the current letter's brand.
        cur_prof = profiles[i] if i < len(profiles) else {}
        prev_prof = profiles[i - 1] if i - 1 < len(profiles) else {}
        cur_cue = _match_start_cue(t, cur_prof, cues)
        prev_cue = _match_start_cue(prev, prev_prof, cues)
        doc_key = _sender_key(page_texts[starts[-1]] if starts else prev)
        cur_key = _sender_key(t)
        if cur_cue and not (
            cur_key and doc_key and cur_key == doc_key and not _almost_same_opening(t, prev)
        ):
            different_brand = bool(
                prev_cue
                and cur_cue.sender_key
                and prev_cue.sender_key
                and cur_cue.sender_key != prev_cue.sender_key
            )
            after_reverse = _looks_like_continuation_page(prev)
            new_logo = bool(cur_cue and not prev_cue)
            phrase_changed = (
                _header_phrase(t)
                and _header_phrase(prev)
                and _phrase_sim(_header_phrase(t), _header_phrase(prev)) < 0.55
            )
            if different_brand or after_reverse or new_logo or (cur_cue and prev_cue and phrase_changed):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = (
                        f"learned letterhead ({cur_cue.sender_key or cur_cue.header_phrase or 'start'})"
                    )
                continue

        # Two copies of the same 1-page form (near-identical openings / pages)
        if _almost_same_opening(t, prev):
            if i not in starts:
                starts.append(i)
                reasons[i] = "duplicate 1-page form"
            continue
        if i < len(profiles) and i - 1 < len(profiles):
            prev_idx = i - 1
            while prev_idx > 0 and blank(prev_idx):
                prev_idx -= 1
            full_s = _visual_sim(
                (profiles[i].get("full") or []),
                (profiles[prev_idx].get("full") or []),
            )
            if full_s >= 0.985 and not blank(i) and not blank(prev_idx):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "near-identical page (second copy)"
                continue

        # Image-only: letterhead / layout change (same letterhead ⇒ keep together)
        if i < len(profiles) and i - 1 < len(profiles):
            # Look across a preceding blank to the last content page for letterhead
            prev_idx = i - 1
            while prev_idx > 0 and blank(prev_idx):
                prev_idx -= 1
            cur, prev_p = profiles[i], profiles[prev_idx]
            top_s = _visual_sim(cur.get("top") or [], prev_p.get("top") or [])
            full_s = _visual_sim(cur.get("full") or [], prev_p.get("full") or [])
            # Strong same letterhead band → continuation of multi-page letter
            if top_s >= 0.78:
                pass  # do not split on layout alone
            elif top_s < 0.72 and full_s < 0.84 and _strong_new_letter_start(t):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = f"letterhead change (top={top_s:.2f})"
                continue
            elif top_s < 0.72 and full_s < 0.84:
                # Layout changed but no strong new-letter cue — keep (safer for packs)
                pass

        if _page_looks_like_new_document(t, is_first=False):
            head = t[:500]
            page1 = bool(re.search(r"\bPage\s*1\s*(?:of|/)\s*\d+\b", head, re.I))
            strong_new = _strong_new_letter_start(t)
            prev_cont = bool(
                re.search(r"\bPage\s*[2-9]\d*\s*(?:of|/)\s*\d+\b", prev[:400], re.I)
            )
            start_key = _sender_key(page_texts[starts[-1]] if starts else prev)
            cur_key = _sender_key(t)
            same_sender = bool(start_key and cur_key and start_key == cur_key)
            if prev_cont and not page1 and not (strong_new and not same_sender):
                continue
            if same_sender and not page1:
                # Same brand as the current letter (e.g. TPR pages 2–4)
                continue
            if strong_new or (page1 and strong_new):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "letter/form marker" if not page1 else "page 1 of N"
            elif page1 and not prev_cont:
                # Page 1 of N alone is weak after mid-pack noise — require extra cue
                if re.search(r"\bDear\s+|\bHMRC\b|\bInvoice\b|\bForm\s+SA", head, re.I):
                    if i not in starts:
                        starts.append(i)
                        reasons[i] = "page 1 of N + letter cue"
            elif not prev_cont and re.search(
                r"\bHM\s*Revenue|\bHMRC\b|\bCompanies\s+House\b", head, re.I
            ):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "new letterhead text"

    starts = sorted(set(starts))
    ranges: List[Tuple[int, int, str]] = []
    for idx, st in enumerate(starts):
        # Keep leading blanks only if they are the very start; otherwise attach
        # blanks to the previous document (they sit before this start index).
        if st >= n:
            continue
        if idx + 1 < len(starts):
            en = starts[idx + 1] - 1
        else:
            en = n - 1
        # Include blank pages inside the range (do NOT strip — was dropping
        # court blank pages and making packs "incomplete")
        if en < st:
            continue
        ranges.append((st + 1, en + 1, reasons.get(starts[idx], "split")))

    if not ranges:
        return [(1, n, "whole file")]
    return ranges


def parse_page_ranges_spec(spec: str, page_count: int) -> List[Tuple[int, int, str]]:
    """
    Parse user page ranges like: 1-8, 10, 12-19, 21-24
    Returns list of (start, end, reason) 1-based inclusive, validated & sorted.
    """
    raw = (spec or "").strip()
    if not raw:
        return []
    out: List[Tuple[int, int, str]] = []
    for part in re.split(r"[,;\n]+", raw):
        part = part.strip().replace("–", "-").replace("—", "-")
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not a.strip().isdigit() or not b.strip().isdigit():
                continue
            ps, pe = int(a.strip()), int(b.strip())
        elif part.isdigit():
            ps = pe = int(part)
        else:
            continue
        if ps < 1:
            ps = 1
        if pe > page_count:
            pe = page_count
        if pe < ps:
            ps, pe = pe, ps
        out.append((ps, pe, "manual range"))
    out.sort(key=lambda x: (x[0], x[1]))
    # drop overlaps by keeping first-listed order after sort — clip overlaps
    cleaned: List[Tuple[int, int, str]] = []
    last_end = 0
    for ps, pe, reason in out:
        if pe <= last_end:
            continue
        if ps <= last_end:
            ps = last_end + 1
        if ps > pe:
            continue
        cleaned.append((ps, pe, reason))
        last_end = pe
    return cleaned


def parse_letter_page_counts(
    spec: str, page_count: int
) -> Tuple[List[int], str]:
    """
    Parse a letter list: '2,2,1,4' or shorthand '8x2, 2x1, 4, 2'.

    Returns (page_counts, error). Empty error means ok. Counts must sum
    exactly to page_count.
    """
    raw = (spec or "").strip()
    if not raw:
        return [], "Enter how many pages each letter has"
    counts: List[int] = []
    for part in re.split(r"[,;\n]+", raw):
        part = part.strip().lower().replace(" ", "")
        if not part:
            continue
        part = part.replace("×", "x").replace("*", "x")
        if "x" in part:
            a, _, b = part.partition("x")
            if not a.isdigit() or not b.isdigit():
                return [], f"Cannot read '{part}' — use 8x2 or 2"
            n, size = int(a), int(b)
            if n < 1 or n > 80 or size < 1 or size > page_count:
                return [], f"Cannot read '{part}'"
            counts.extend([size] * n)
        elif part.isdigit():
            n = int(part)
            if n < 1 or n > page_count:
                return [], f"Letter of {n} page(s) is not valid"
            counts.append(n)
        else:
            return [], f"Cannot read '{part}' — use 2 or 8x2"
    if not counts:
        return [], "Enter how many pages each letter has"
    if len(counts) > 80:
        return [], "Too many letters (max 80)"
    total = sum(counts)
    if total != page_count:
        if total < page_count:
            return counts, f"Pages add up to {total}, but the scan has {page_count}"
        return counts, f"Pages add up to {total}, but the scan only has {page_count}"
    return counts, ""


def page_counts_to_ranges(
    counts: List[int], *, reason: str = "letter list"
) -> List[Tuple[int, int, str]]:
    ranges: List[Tuple[int, int, str]] = []
    cur = 1
    for i, n in enumerate(counts):
        if n < 1:
            continue
        ranges.append((cur, cur + n - 1, f"{reason} · letter {i + 1}"))
        cur += n
    return ranges


def learn_split_pattern(db: Session, counts: List[int]) -> Optional[PostSplitLesson]:
    """Remember a human-confirmed page-count list so later imports can reuse it."""
    counts = [int(c) for c in counts if int(c) > 0]
    if len(counts) < 2:
        return None
    key = ",".join(str(c) for c in counts)
    total = sum(counts)
    row = (
        db.query(PostSplitLesson)
        .filter(
            PostSplitLesson.total_pages == total,
            PostSplitLesson.page_counts == key,
        )
        .first()
    )
    if row:
        row.hit_count = int(row.hit_count or 0) + 1
        row.updated_at = datetime.utcnow()
    else:
        row = PostSplitLesson(
            total_pages=total,
            letter_count=len(counts),
            page_counts=key,
            hit_count=1,
            notes="Taught from letter list",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        return None
    return row


def suggest_split_counts(
    db: Session, page_count: int
) -> Optional[List[int]]:
    """Best learned page-count list for a scan of this length."""
    if page_count < 2:
        return None
    row = (
        db.query(PostSplitLesson)
        .filter(PostSplitLesson.total_pages == page_count)
        .order_by(
            PostSplitLesson.hit_count.desc(),
            PostSplitLesson.updated_at.desc(),
        )
        .first()
    )
    if not row or not row.page_counts:
        return None
    try:
        counts = [int(x) for x in row.page_counts.split(",") if x.strip().isdigit()]
    except Exception:
        return None
    if sum(counts) != page_count or len(counts) < 2:
        return None
    return counts


def apply_learned_split_if_needed(
    db: Session,
    page_count: int,
    auto_ranges: List[Tuple[int, int, str]],
) -> List[Tuple[int, int, str]]:
    """
    If auto-split collapsed a mixed pile to one blob (or badly disagrees
    with a taught pattern), use the learned letter list.
    """
    learned = suggest_split_counts(db, page_count)
    if not learned:
        return auto_ranges
    auto_n = len(auto_ranges or [])
    # One blob vs a taught multi-letter cut — always prefer the lesson
    if auto_n <= 1 and len(learned) > 1:
        return page_counts_to_ranges(learned, reason="learned letter list")
    row = (
        db.query(PostSplitLesson)
        .filter(
            PostSplitLesson.total_pages == page_count,
            PostSplitLesson.page_counts == ",".join(str(c) for c in learned),
        )
        .first()
    )
    hits = int(row.hit_count or 0) if row else 0
    if hits >= 2 and auto_n != len(learned):
        return page_counts_to_ranges(learned, reason="learned letter list")
    return auto_ranges


def current_batch_page_counts(db: Session, batch_id: int) -> List[int]:
    items = (
        db.query(PostItem)
        .filter(
            PostItem.batch_id == batch_id,
            PostItem.status != "holding",
        )
        .order_by(PostItem.sort_order.asc(), PostItem.page_start.asc())
        .all()
    )
    counts: List[int] = []
    for it in items:
        ps = int(it.page_start or 0)
        pe = int(it.page_end or 0)
        if ps and pe and pe >= ps:
            counts.append(pe - ps + 1)
    return counts


def list_open_split_batches(db: Session) -> List[Dict[str, Any]]:
    """Batches sitting in review that can be cut with the letter list."""
    items = list_open_items(db, limit=80)
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        bid = it.batch_id
        if not bid or bid in seen or not it.batch:
            continue
        seen.add(bid)
        batch = it.batch
        n = int(batch.page_count or 0)
        if n < 2:
            continue
        current = current_batch_page_counts(db, bid)
        out.append(
            {
                "batch": batch,
                "page_count": n,
                "current_counts": current,
                "suggested_counts": None,
                "open_letters": len(current),
            }
        )
    return out


def apply_letter_list_split(
    db: Session, batch_id: int, spec: str
) -> Tuple[bool, str]:
    """Cut a scan using a letter page-count list and remember the letterheads."""
    batch = db.query(PostBatch).filter(PostBatch.id == batch_id).first()
    if not batch:
        return False, "Batch not found"
    n = int(batch.page_count or 0)
    if n < 1:
        return False, "Scan has no pages"
    counts, err = parse_letter_page_counts(spec, n)
    if err:
        return False, err
    ranges = page_counts_to_ranges(counts, reason="letter list")
    spec_ranges = ", ".join(
        f"{a}-{b}" if a != b else str(a) for a, b, _ in ranges
    )
    ok, msg = reprocess_batch(db, batch_id, ranges_spec=spec_ranges)
    if not ok:
        return False, msg
    taught = learn_split_cues_from_items(db, batch_id)
    extra = ""
    if taught:
        extra = f" · remembered {taught} letterhead(s) for the next pile"
    return True, f"{msg}{extra}"


def _split_pdf_pages(
    source: Path, dest: Path, page_start: int, page_end: int
) -> Tuple[bool, str]:
    """Write pages page_start..page_end (1-based inclusive) to dest."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter  # type: ignore
        except ImportError:
            return False, "pypdf not installed"

    try:
        reader = PdfReader(str(source), strict=False)
        writer = PdfWriter()
        n = len(reader.pages)
        start = max(1, page_start) - 1
        end = min(n, page_end)
        if start >= n or end < start + 1:
            # allow single page: start inclusive, end exclusive in loop → end must be > start
            if start < n and page_start == page_end:
                end = start + 1
            else:
                return False, "Invalid page range"
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            writer.write(f)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _title_from_text(text: str, fallback: str) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if 8 <= len(s) <= 100 and not s.lower().startswith("page "):
            return s[:200]
    return (fallback or "Document")[:200]


def _build_items_for_batch(
    db: Session,
    batch: PostBatch,
    source_pdf: Path,
    page_texts: List[str],
    dirs: Dict[str, Path],
    *,
    ranges: Optional[List[Tuple[int, int, str]]] = None,
    ink_ratios: Optional[List[float]] = None,
    visual_profiles: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Create PostItem rows for ranges; returns count created."""
    n_pages = len(page_texts) or batch.page_count or 1
    profiles = visual_profiles
    if profiles is None and source_pdf and Path(source_pdf).is_file():
        profiles = extract_page_visual_profiles(Path(source_pdf))
    if ink_ratios is None and profiles:
        ink_ratios = [p.get("ink", 0.5) for p in profiles]
    elif ink_ratios is None and source_pdf and Path(source_pdf).is_file():
        ink_ratios = extract_page_ink_ratios(Path(source_pdf))
    # Image-only scans: empty OCR text but ink on letter pages
    if page_texts and ink_ratios:
        page_texts = _normalize_page_texts_with_ink(page_texts, ink_ratios)
    if ranges is None:
        cues = list_split_cues(db)
        ranges = (
            detect_document_page_ranges(
                page_texts,
                ink_ratios,
                visual_profiles=profiles,
                split_cues=cues,
            )
            if page_texts
            else [(1, n_pages, "whole")]
        )
    if not ranges:
        ranges = [(1, n_pages, "whole")]

    created = 0
    for idx, (ps, pe, reason) in enumerate(ranges):
        text = "\n".join(page_texts[ps - 1 : pe]) if page_texts else ""
        out = dirs["splits"] / f"batch{batch.id}_p{ps}-{pe}.pdf"
        if source_pdf.suffix.lower() == ".pdf" and source_pdf.is_file():
            ok, err = _split_pdf_pages(source_pdf, out, ps, pe)
            if not ok:
                # fall back: copy whole file once
                if idx == 0:
                    out = dirs["splits"] / f"batch{batch.id}_full.pdf"
                    shutil.copy2(str(source_pdf), str(out))
                else:
                    logger.warning("Split pages %s-%s failed: %s", ps, pe, err)
                    continue
        else:
            out = dirs["splits"] / f"batch{batch.id}_full{source_pdf.suffix.lower()}"
            if not out.exists():
                shutil.copy2(str(source_pdf), str(out))

        rule, action, category, conf = apply_rules(db, text)
        client, cconf, match_reason = suggest_client(db, text)
        if rule and rule.client_id and not client:
            client = db.query(Client).filter(Client.id == rule.client_id).first()
            if client:
                match_reason = f"Rule: {rule.name}"
                cconf = max(cconf, 0.7)

        title = _title_from_text(text, f"{batch.original_filename} p{ps}-{pe}")
        if len(ranges) > 1:
            match_reason = (
                f"{match_reason + ' · ' if match_reason else ''}Auto-split: {reason}"
            )

        item = PostItem(
            batch_id=batch.id,
            sort_order=idx,
            title=title,
            page_start=ps,
            page_end=pe,
            local_path=str(out),
            content_type="application/pdf"
            if out.suffix.lower() == ".pdf"
            else f"image/{out.suffix.lower().lstrip('.')}",
            size_bytes=int(out.stat().st_size) if out.is_file() else 0,
            text_excerpt=(text or "")[:8000] or None,
            category=category,
            suggested_action=action,
            suggested_client_id=client.id if client else None,
            confidence=max(conf, cconf) if (conf or cconf) else None,
            match_reason=match_reason or None,
            status="suggested" if action != "review" and client else "inbox",
            rule_id=rule.id if rule else None,
            created_at=datetime.utcnow(),
        )
        db.add(item)
        created += 1
    return created


def _find_company_numbers(text: str) -> List[str]:
    found: List[str] = []
    for m in re.finditer(
        r"\b((?:SC|NI|OC|SO|R0|RS)?\d{6,8})\b", text or "", re.I
    ):
        cn = normalize_company_number(m.group(1))
        if cn and cn not in found:
            found.append(cn)
    return found[:8]


def _keyword_hits(text: str, keywords: tuple) -> List[str]:
    low = (text or "").lower()
    return [k for k in keywords if k in low]


def seed_default_rules(db: Session) -> int:
    """Install starter rules if none exist; also ensure court/legal rule exists."""
    created = 0
    n = db.query(PostRule).count()
    if n == 0:
        starters = [
            PostRule(
                name="HMRC standard correspondence",
                keywords="\n".join(_HMRC_KEYWORDS[:10]),
                match_mode="any",
                action="file_hmrc",
                category="HMRC",
                priority=200,
                learned=False,
                notes="File to client HMRC/Tax correspondence folder",
            ),
            PostRule(
                name="Payment chaser / demand",
                keywords="\n".join(_CHASE_KEYWORDS[:10]),
                match_mode="any",
                action="email_client",
                category="Chase / demand",
                priority=180,
                learned=False,
                notes="Forward to client contact for action",
            ),
            PostRule(
                name="Companies House",
                keywords="\n".join(_CH_KEYWORDS),
                match_mode="any",
                action="file_client",
                category="Companies House",
                priority=170,
                learned=False,
            ),
        ]
        for r in starters:
            db.add(r)
        created += len(starters)

    # Always ensure court/legal routing exists (filing category; split logic
    # also keeps packs together when these keywords appear in OCR).
    court_name = "Court / legal pack"
    has_court = (
        db.query(PostRule).filter(PostRule.name == court_name).first() is not None
    )
    if not has_court:
        db.add(
            PostRule(
                name=court_name,
                keywords="\n".join(
                    [
                        "county court",
                        "high court",
                        "claim form",
                        "particulars of claim",
                        "witness statement",
                        "statement of truth",
                        "claimant",
                        "defendant",
                        "claim number",
                        "tribunal",
                    ]
                ),
                match_mode="any",
                action="file_client",
                category="Client correspondence",
                priority=190,
                learned=True,
                notes=(
                    "Court packs often include intentional blank pages — "
                    "auto-split keeps them as one document."
                ),
            )
        )
        created += 1

    if created:
        db.commit()
    return created


def learn_keep_together_from_item(db: Session, item: PostItem) -> None:
    """
    When a user forces a multi-page item to stay as one document, remember
    keywords from its text so future court/legal-like packs classify correctly.
    """
    text = (item.text_excerpt or "").strip()
    if len(text) < 40:
        return
    # Prefer court-ish tokens from the text
    low = text.lower()
    found = [k for k in _COURT_LEGAL_KEYWORDS if k in low][:8]
    if not found:
        words = re.findall(r"[A-Za-z][A-Za-z&]{4,}", text[:800])
        found = list(dict.fromkeys(w.lower() for w in words))[:6]
    if not found:
        return
    name = "Learned keep-together (court/pack)"
    rule = db.query(PostRule).filter(PostRule.name == name).first()
    if rule:
        existing = {
            k.strip().lower()
            for k in re.split(r"[\n,;]+", rule.keywords or "")
            if k.strip()
        }
        for k in found:
            existing.add(k)
        rule.keywords = "\n".join(sorted(existing)[:40])
        rule.hit_count = int(rule.hit_count or 0) + 1
        rule.updated_at = datetime.utcnow()
        rule.is_active = True
    else:
        db.add(
            PostRule(
                name=name,
                keywords="\n".join(found),
                match_mode="any",
                action="file_client",
                category="Client correspondence",
                priority=185,
                learned=True,
                hit_count=1,
                notes="Learned when a multipage scan was kept as one document",
            )
        )
    try:
        db.commit()
    except Exception:
        db.rollback()


def list_rules(db: Session, *, active_only: bool = False) -> List[PostRule]:
    q = db.query(PostRule).order_by(PostRule.priority.desc(), PostRule.id.asc())
    if active_only:
        q = q.filter(PostRule.is_active.is_(True))
    return q.all()


def apply_rules(
    db: Session, text: str
) -> Tuple[Optional[PostRule], str, str, float]:
    """
    Returns (rule, action, category, confidence).
    """
    low = (text or "").lower()
    if not low.strip():
        return None, "review", "Other", 0.0

    if _is_practice_mail(text):
        return None, "file_and_task", "Personal / practice", 0.9

    for rule in list_rules(db, active_only=True):
        kws = [
            k.strip().lower()
            for k in re.split(r"[\n,;]+", rule.keywords or "")
            if k.strip()
        ]
        if not kws:
            continue
        if rule.match_mode == "all":
            hit = all(k in low for k in kws)
        else:
            hit = any(k in low for k in kws)
        if not hit:
            continue
        conf = 0.55 + min(0.35, 0.05 * sum(1 for k in kws if k in low))
        return (
            rule,
            rule.action or "review",
            rule.category or "Other",
            conf,
        )

    # Built-in fallbacks if no DB rule hit
    if _keyword_hits(low, _HMRC_KEYWORDS):
        return None, "file_hmrc", "HMRC", 0.5
    if _keyword_hits(low, _CHASE_KEYWORDS):
        return None, "email_client", "Chase / demand", 0.45
    if _keyword_hits(low, _CH_KEYWORDS):
        return None, "file_client", "Companies House", 0.45
    return None, "review", "Other", 0.1


def suggest_client(
    db: Session, text: str
) -> Tuple[Optional[Client], float, str]:
    """Match client by company number in text, letterhead name, or reverse name scan."""
    raw = text or ""
    for cn in _find_company_numbers(raw):
        c = (
            db.query(Client)
            .filter(Client.company_number == cn)
            .first()
        )
        if c:
            return c, 0.95, f"Company number {cn}"

    # Try significant capitalised phrases as names (cheap heuristic)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    candidates: List[str] = []
    for ln in lines[:60]:
        if len(ln) < 4 or len(ln) > 100:
            continue
        if re.search(r"\b(limited|ltd|llp|plc|llp)\b", ln, re.I):
            candidates.append(ln)
        # Also lines in ALL CAPS that look like company names
        elif len(ln) >= 8 and ln.upper() == ln and re.search(r"[A-Z]{3,}", ln):
            candidates.append(ln.title())
    for name in candidates[:12]:
        ranked = match_clients_ranked(db, name, limit=1)
        if ranked and ranked[0][1] >= 65:
            return ranked[0][0], min(0.9, ranked[0][1] / 100.0), f"Name ~ {name[:40]}"

    # Reverse scan: does any practice client name appear in the letter text?
    # Works well once OCR/vision has read the letterhead.
    low = normalize_client_name(raw)
    if len(low) >= 20:
        best: Optional[Tuple[Client, float, str]] = None
        clients = (
            db.query(Client)
            .filter(Client.overall_status.notin_(["Inactive", "Former"]))
            .all()
        )
        for c in clients:
            name = normalize_client_name(c.company_name or "")
            if len(name) < 5:
                continue
            # Drop very common short suffixes-only matches
            core = re.sub(
                r"\b(limited|ltd|llp|plc|the|and|&)\b", " ", name
            )
            core = re.sub(r"\s+", " ", core).strip()
            if len(core) < 4:
                continue
            if name in low or (len(core) >= 6 and core in low):
                score = 0.88 if name in low else 0.78
                # Prefer longer / more specific names
                score += min(0.08, len(core) / 200.0)
                if best is None or score > best[1]:
                    best = (c, score, f"Letter mentions {c.company_name}")
        if best and best[1] >= 0.78:
            return best[0], min(0.95, best[1]), best[2]

    return None, 0.0, ""


def _pdf_page_png_b64(path: Path, page_index: int = 0, dpi: float = 140) -> Optional[str]:
    """Render one PDF page to PNG base64 for vision OCR."""
    try:
        import base64

        import fitz
    except ImportError:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    try:
        doc = fitz.open(str(path))
        if page_index < 0 or page_index >= doc.page_count:
            doc.close()
            return None
        zoom = max(1.0, dpi / 72.0)
        pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        data = pix.tobytes("png")
        doc.close()
        return base64.b64encode(data).decode("ascii")
    except Exception as exc:
        logger.warning("page render failed %s p%s: %s", path, page_index, exc)
        return None


def ocr_pdf_with_vision(path: Path, *, max_pages: int = 2) -> str:
    """
    Read text from image-only scan pages via xAI vision (Grok).
    Used when pdfplumber/pypdf extract nothing (typical scanner PDFs).
    """
    from app.config import AI_MODEL, XAI_API_KEY

    if not (XAI_API_KEY or "").strip():
        return ""
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        import fitz
        import httpx
    except ImportError:
        return ""

    try:
        n = fitz.open(str(path)).page_count
    except Exception:
        n = 1
    n = min(max(1, n), max_pages)

    chunks: List[str] = []
    for i in range(n):
        b64 = _pdf_page_png_b64(path, i, dpi=130)
        if not b64:
            continue
        prompt = (
            "You are reading a scanned UK accounting practice letter or form. "
            "Transcribe the visible text faithfully. Prioritise: company/recipient "
            "name, company number, address, sender (HMRC, Companies House, bank, etc.), "
            "subject line, and any reference numbers. "
            "Return plain text only — no markdown fences."
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        try:
            headers = {
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            }
            body = {
                "model": AI_MODEL or "grok-4.5",
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 1200,
            }
            with httpx.Client(timeout=90.0) as client:
                r = client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers=headers,
                    json=body,
                )
                if r.status_code >= 400:
                    logger.warning(
                        "vision OCR HTTP %s: %s", r.status_code, r.text[:200]
                    )
                    continue
                data = r.json()
            text = (
                ((data.get("choices") or [{}])[0].get("message") or {}).get(
                    "content"
                )
                or ""
            ).strip()
            if text:
                chunks.append(text)
        except Exception as exc:
            logger.warning("vision OCR failed %s p%s: %s", path.name, i + 1, exc)
    return "\n\n".join(chunks)[:20000]


def _page_has_real_text(text: str) -> bool:
    s = re.sub(r"\s+", "", text or "")
    if s.startswith("[scannedpage"):
        return False
    return len(s) >= 30


def _pages_need_ocr(page_texts: List[str]) -> bool:
    if not page_texts:
        return True
    real = sum(1 for t in page_texts if _page_has_real_text(t))
    return real < max(1, (len(page_texts) + 2) // 3)


def ocr_pdf_pages_for_split(path: Path, page_count: int) -> List[str]:
    """
    Per-page vision OCR for image-only scanner PDFs, used *before* auto-split.

    Split used to run on an empty text layer, so mixed post (different logos /
    typefaces) was left as one 24-page blob. We only need the header + opening.
    """
    from app.config import AI_MODEL, XAI_API_KEY

    out = [""] * max(page_count, 0)
    if page_count < 1 or not (XAI_API_KEY or "").strip():
        return out
    path = Path(path)
    if not path.is_file():
        return out
    try:
        import httpx
    except ImportError:
        return out

    prompt = (
        "These images are consecutive pages of mixed UK practice post "
        "(several letters in one scan). For EACH image, transcribe the header "
        "and opening only: sender/logo name, recipient, subject, page X of Y, "
        "Dear line, and the first few sentences. "
        "Prefix every page exactly as ===PAGE n=== where n is the page number "
        "shown in the user text. Plain text only."
    )

    batch_size = 3
    for start in range(0, page_count, batch_size):
        idxs = list(range(start, min(start + batch_size, page_count)))
        content: List[Dict[str, Any]] = []
        used: List[int] = []
        for i in idxs:
            b64 = _pdf_page_png_b64(path, i, dpi=110)
            if not b64:
                continue
            used.append(i)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
            content.append(
                {
                    "type": "text",
                    "text": f"This image is page {i + 1} of {page_count}.",
                }
            )
        if not used:
            continue
        content.append({"type": "text", "text": prompt})
        try:
            headers = {
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            }
            body = {
                "model": AI_MODEL or "grok-4.5",
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.1,
                "max_tokens": 1800,
            }
            with httpx.Client(timeout=120.0) as client:
                r = client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers=headers,
                    json=body,
                )
                if r.status_code >= 400:
                    logger.warning(
                        "split-OCR HTTP %s: %s", r.status_code, r.text[:200]
                    )
                    continue
                data = r.json()
            text = (
                ((data.get("choices") or [{}])[0].get("message") or {}).get(
                    "content"
                )
                or ""
            ).strip()
        except Exception as exc:
            logger.warning("split-OCR batch %s failed: %s", start + 1, exc)
            continue

        # Parse ===PAGE n=== blocks; fall back to assigning the whole blob
        found = {
            int(m.group(1)): (m.group(2) or "").strip()
            for m in re.finditer(
                r"===PAGE\s*(\d+)\s*===\s*(.*?)(?====PAGE\s*\d+\s*===|\Z)",
                text,
                re.I | re.S,
            )
        }
        if found:
            for num, body_txt in found.items():
                if 1 <= num <= page_count and body_txt:
                    out[num - 1] = body_txt[:4000]
        elif len(used) == 1:
            out[used[0]] = text[:4000]
        else:
            # Last resort: one page at a time for this batch
            for i in used:
                if out[i]:
                    continue
                single = ocr_pdf_with_vision(path, max_pages=1) if i == 0 else ""
                if i == 0 and single:
                    out[0] = single[:4000]
    return out


def fill_page_texts_for_split(
    path: Path, page_texts: List[str]
) -> List[str]:
    """OCR empty image-only pages so auto-split can see letterheads."""
    n = len(page_texts)
    if n == 0 or not _pages_need_ocr(page_texts):
        return page_texts
    logger.info("Split OCR starting for %s (%s image-only pages)", path.name, n)
    ocr_pages = ocr_pdf_pages_for_split(path, n)
    merged: List[str] = []
    for i in range(n):
        existing = page_texts[i] if i < len(page_texts) else ""
        if _page_has_real_text(existing):
            merged.append(existing)
        else:
            merged.append((ocr_pages[i] if i < len(ocr_pages) else "") or existing)
    return merged


def enrich_item_text_and_match(
    db: Session, item: PostItem, *, use_vision: bool = True
) -> bool:
    """OCR if needed, re-run rules + client match. Returns True if updated."""
    ensure_item_file(db, item)
    path = Path(item.local_path) if item.local_path else None
    text = (item.text_excerpt or "").strip()
    placeholder = text.startswith("[scanned") or len(text) < 40

    if path and path.is_file() and (placeholder or not text) and use_vision:
        ocr = ocr_pdf_with_vision(path, max_pages=2)
        if ocr and len(ocr) > 40:
            text = ocr
            item.text_excerpt = ocr[:8000]
            # Prefer a real title from first line of OCR
            title = _title_from_text(ocr, item.title or path.name)
            if title and not title.startswith("Scan"):
                item.title = title[:200]

    if not text or len(text) < 20:
        return False

    rule, action, category, conf = apply_rules(db, text)
    client, cconf, match_reason = suggest_client(db, text)
    if _is_practice_mail(text):
        action = "file_and_task"
        category = "Personal / practice"
        conf = max(conf, 0.9)
        prac = find_practice_client(db, text)
        if prac:
            client, cconf = prac, max(cconf, 0.92)
            match_reason = f"Addressed to the practice ({prac.display_name()})"
        else:
            match_reason = "Addressed to Accology — practice mail (file + task)"
    if rule and rule.client_id and not client:
        client = db.query(Client).filter(Client.id == rule.client_id).first()
        if client:
            match_reason = f"Rule: {rule.name}"
            cconf = max(cconf, 0.7)

    item.category = category or item.category
    item.suggested_action = action or item.suggested_action
    if client:
        item.suggested_client_id = client.id
    # Keep confidence meaningful after OCR even if no client yet
    if text and len(text) > 80:
        conf = max(conf, 0.4)
    if cconf:
        conf = max(conf, cconf)
    item.confidence = conf if conf else item.confidence
    if match_reason:
        item.match_reason = match_reason
    item.rule_id = rule.id if rule else item.rule_id
    if client:
        item.status = "suggested"
    elif item.status not in ("inbox", "suggested"):
        pass
    else:
        item.status = "inbox"
    return True


def reclassify_open_items(
    db: Session, *, limit: int = 40, use_vision: bool = True
) -> Dict[str, Any]:
    """Re-OCR and re-match open post items (fixes 10% confidence / no client)."""
    items = (
        db.query(PostItem)
        .options(joinedload(PostItem.batch))
        .filter(PostItem.status.in_(["inbox", "suggested"]))
        .order_by(PostItem.id.desc())
        .limit(limit)
        .all()
    )
    updated = 0
    matched = 0
    errors: List[str] = []
    for it in items:
        try:
            if enrich_item_text_and_match(db, it, use_vision=use_vision):
                updated += 1
                if it.suggested_client_id:
                    matched += 1
        except Exception as exc:
            errors.append(f"#{it.id}: {exc}")
            logger.exception("reclassify item %s", it.id)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return {
            "ok": False,
            "updated": 0,
            "matched": 0,
            "errors": [str(exc)],
        }
    return {
        "ok": True,
        "updated": updated,
        "matched": matched,
        "total": len(items),
        "errors": errors,
    }


def normalize_scan_pdf(
    path: Path,
    *,
    reverse_order: Optional[bool] = None,
    rotate_180: Optional[bool] = None,
) -> Tuple[bool, str]:
    """
    Fix feeder quirks common on Brother document scanners:
      - reverse page order (ADF takes from back of stack)
      - rotate each page 180° (saved upside down)

    Rewrites *path* in place. Returns (ok, message).
    """
    from app import config

    path = Path(path)
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return True, "skip non-pdf"
    do_rev = (
        bool(getattr(config, "POST_SCAN_REVERSE_ORDER", True))
        if reverse_order is None
        else bool(reverse_order)
    )
    do_rot = (
        bool(getattr(config, "POST_SCAN_ROTATE_180", True))
        if rotate_180 is None
        else bool(rotate_180)
    )
    if not do_rev and not do_rot:
        return True, "no transform"

    # Prefer PyMuPDF
    try:
        import fitz

        src = fitz.open(str(path))
        n = src.page_count
        if n < 1:
            src.close()
            return True, "empty"
        out = fitz.open()
        indices = list(range(n))
        if do_rev:
            indices = list(reversed(indices))
        for i in indices:
            out.insert_pdf(src, from_page=i, to_page=i)
            if do_rot:
                # Combine with any existing rotation
                cur = out[-1].rotation
                out[-1].set_rotation((cur + 180) % 360)
        tmp = path.with_suffix(".norm.pdf")
        out.save(str(tmp), garbage=3, deflate=True)
        out.close()
        src.close()
        tmp.replace(path)
        bits = []
        if do_rev:
            bits.append("reversed")
        if do_rot:
            bits.append("rotated-180")
        return True, f"{'+'.join(bits)} ({n} pages)"
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("fitz normalize failed, trying pypdf: %s", exc)

    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(path), strict=False)
        writer = PdfWriter()
        pages = list(reader.pages)
        if do_rev:
            pages = list(reversed(pages))
        for pg in pages:
            if do_rot:
                try:
                    pg.rotate(180)
                except Exception:
                    pass
            writer.add_page(pg)
        tmp = path.with_suffix(".norm.pdf")
        with tmp.open("wb") as f:
            writer.write(f)
        tmp.replace(path)
        return True, f"pypdf reverse={do_rev} rotate={do_rot} pages={len(pages)}"
    except Exception as exc:
        logger.exception("normalize_scan_pdf %s", path)
        return False, str(exc)


def reimport_from_done(
    db: Session, *, limit: int = 20, force: bool = True
) -> Dict[str, Any]:
    """
    Pull PDFs from the processed (done/) folder back through import.

    After a bad auto-split, files sit in done/ and normal Import skips them
    by content hash. force=True allows re-import when the old batch has no
    open review items (or always creates a fresh batch by clearing hash match
    for empty batches).
    """
    dirs = ensure_inbox_dirs()
    if not dirs["done"].is_dir():
        return {
            "ok": False,
            "moved": 0,
            "errors": ["done/ folder missing"],
            "inbox_path": str(dirs["inbox"]),
        }

    moved = 0
    errors: List[str] = []
    for p in sorted(
        dirs["done"].iterdir(), key=lambda x: x.stat().st_mtime, reverse=True
    ):
        if moved >= limit:
            break
        if not p.is_file():
            continue
        if p.suffix.lower() not in {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
            ".webp",
        }:
            continue
        try:
            h = _file_hash(p)
            existing = (
                db.query(PostBatch).filter(PostBatch.content_hash == h).first()
            )
            if existing and force:
                open_n = (
                    db.query(PostItem)
                    .filter(
                        PostItem.batch_id == existing.id,
                        PostItem.status.in_(
                            ["inbox", "suggested", "error", "holding"]
                        ),
                    )
                    .count()
                )
                if open_n == 0:
                    # Free the hash so import can run again
                    existing.content_hash = f"{h}_superseded_{existing.id}"
                    existing.status = "reviewed"
                    db.commit()
                else:
                    errors.append(
                        f"{p.name}: batch still has {open_n} open item(s) — clear them first"
                    )
                    continue
            elif existing and not force:
                errors.append(f"{p.name}: already imported (hash match)")
                continue

            dest = dirs["inbox"] / p.name
            if dest.exists():
                dest = dirs["inbox"] / f"{p.stem}_reimport{p.suffix}"
            shutil.move(str(p), str(dest))
            moved += 1
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")

    result = import_from_inbox(db, limit=limit)
    result["moved_from_done"] = moved
    result["reimport_errors"] = errors
    result["ok"] = True
    return result


def import_from_inbox(db: Session, *, limit: int = 40) -> Dict[str, Any]:
    """
    Import new files from inbox/ into post_batches + post_items.

    Multi-page PDFs are auto-split into multiple items using content markers
    (letterheads, Dear…, Form SA…, blank separators) — same approach as the
    Ahmed Bros multi-invoice pipeline, adapted for post.

    Court/legal packs with intentional blank pages stay as one document.

    On import, PDFs are normalised for Brother ADF quirks (reverse order + 180°).
    """
    dirs = ensure_inbox_dirs()
    seed_default_rules(db)

    candidates: List[Path] = []
    for folder in (dirs["inbox"], dirs["root"]):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime):
            if not p.is_file():
                continue
            if p.parent == dirs["root"] and p.name.lower() in {
                "desktop.ini",
                "thumbs.db",
            }:
                continue
            if p.parent == dirs["root"] and p.name.lower() in {
                "inbox",
                "processing",
                "done",
                "failed",
                "splits",
            }:
                continue
            ext = p.suffix.lower()
            if ext not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
                continue
            candidates.append(p)
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    imported = 0
    items_created = 0
    skipped = 0
    errors: List[str] = []

    for path in candidates:
        try:
            h = _file_hash(path)
            exists = (
                db.query(PostBatch)
                .filter(PostBatch.content_hash == h)
                .first()
            )
            if exists:
                skipped += 1
                try:
                    dest = dirs["done"] / path.name
                    if path.resolve() != dest.resolve() and path.exists():
                        if dest.exists():
                            dest = dirs["done"] / f"{path.stem}_{h[:8]}{path.suffix}"
                        shutil.move(str(path), str(dest))
                except Exception:
                    pass
                continue

            proc = dirs["processing"] / path.name
            if proc.exists():
                proc = dirs["processing"] / f"{path.stem}_{h[:8]}{path.suffix}"
            shutil.move(str(path), str(proc))

            # Brother ADF: reverse stack order + flip upside-down pages
            if proc.suffix.lower() == ".pdf":
                ok_n, nmsg = normalize_scan_pdf(proc)
                if ok_n and nmsg not in ("no transform", "skip non-pdf", "empty"):
                    logger.info("Scan normalize %s: %s", proc.name, nmsg)
                elif not ok_n:
                    logger.warning("Scan normalize failed %s: %s", proc.name, nmsg)

            page_texts: List[str] = []
            page_count = 1
            ink_ratios: List[float] = []
            visual_profiles: List[Dict[str, Any]] = []
            if proc.suffix.lower() == ".pdf":
                page_texts, _terr = extract_pdf_page_texts(proc)
                page_count = len(page_texts) or 1
                visual_profiles = extract_page_visual_profiles(proc)
                ink_ratios = [p.get("ink", 0.5) for p in visual_profiles]
                if page_count < 1 and ink_ratios:
                    page_count = len(ink_ratios)
                    page_texts = [""] * page_count
                if page_texts and _pages_need_ocr(page_texts):
                    page_texts = fill_page_texts_for_split(proc, page_texts)
            if page_count < 1:
                page_count = 1

            batch = PostBatch(
                original_filename=path.name,
                source_path=str(proc),
                content_hash=h,
                page_count=page_count,
                size_bytes=int(proc.stat().st_size),
                status="processing",
                created_at=datetime.utcnow(),
            )
            db.add(batch)
            db.flush()

            # Archive original first so splits use a stable path
            done = dirs["done"] / f"{batch.id}_{path.name}"
            try:
                shutil.move(str(proc), str(done))
                batch.archived_path = str(done)
                source_for_split = done
            except Exception:
                batch.archived_path = str(proc)
                source_for_split = proc

            n_items = _build_items_for_batch(
                db,
                batch,
                source_for_split,
                page_texts,
                dirs,
                ink_ratios=ink_ratios,
                visual_profiles=visual_profiles,
            )
            items_created += n_items

            batch.status = "ready"
            batch.processed_at = datetime.utcnow()
            db.commit()

            # Image-only scans: vision OCR first page(s) + client match
            try:
                batch_items = (
                    db.query(PostItem)
                    .filter(PostItem.batch_id == batch.id)
                    .order_by(PostItem.sort_order.asc())
                    .all()
                )
                for it in batch_items:
                    enrich_item_text_and_match(db, it, use_vision=True)
                db.commit()
            except Exception:
                logger.exception("OCR/match after import failed for batch %s", batch.id)
                try:
                    db.rollback()
                except Exception:
                    pass

            imported += 1

            try:
                from app.services.notifications import create_notification

                create_notification(
                    db,
                    type="post_inbox",
                    title=f"New post: {path.name}",
                    body=(
                        f"{page_count} page(s) · {n_items} document(s) after auto-split"
                    ),
                    link="/post",
                    entity_type="post_batch",
                    entity_id=batch.id,
                    commit=True,
                )
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Import failed for %s", path)
            errors.append(f"{path.name}: {exc}")
            try:
                db.rollback()
            except Exception:
                pass
            try:
                fail = dirs["failed"] / path.name
                if path.exists():
                    shutil.move(str(path), str(fail))
                elif (dirs["processing"] / path.name).exists():
                    shutil.move(str(dirs["processing"] / path.name), str(fail))
            except Exception:
                pass

    return {
        "ok": True,
        "imported": imported,
        "items_created": items_created,
        "skipped": skipped,
        "errors": errors,
        "inbox_path": str(dirs["inbox"]),
    }


def reprocess_batch(
    db: Session,
    batch_id: int,
    *,
    ranges_spec: str = "",
    replace_all_open: bool = True,
) -> Tuple[bool, str]:
    """
    Re-run auto-split (or apply manual page ranges) on an imported batch.

    ranges_spec examples: \"1-8, 10, 12-19, 21-24\"
    Replaces open inbox/suggested items only — keeps filed/emailed items.
    """
    batch = db.query(PostBatch).filter(PostBatch.id == batch_id).first()
    if not batch:
        return False, "Batch not found"
    src = None
    for cand in (batch.archived_path, batch.source_path):
        if cand and Path(cand).is_file():
            src = Path(cand)
            break
    if not src:
        return False, "Original scan file missing on disk — re-import from Accologise Post"

    dirs = ensure_inbox_dirs()
    page_texts, _err = extract_pdf_page_texts(src)
    profiles = extract_page_visual_profiles(src)
    ink = [p.get("ink", 0.5) for p in profiles]
    if not page_texts and ink:
        page_texts = [""] * len(ink)
    if not page_texts:
        return False, "Could not read PDF pages"
    if not ranges_spec and _pages_need_ocr(page_texts):
        page_texts = fill_page_texts_for_split(src, page_texts)

    n_pages = len(page_texts)
    manual = parse_page_ranges_spec(ranges_spec, n_pages) if ranges_spec else []

    # Pages already claimed by filed/emailed items must not be recreated
    kept = (
        db.query(PostItem)
        .filter(
            PostItem.batch_id == batch_id,
            PostItem.status.notin_(["inbox", "suggested", "error"]),
        )
        .all()
    )
    used_pages: set = set()
    for it in kept:
        ps = int(it.page_start or 0)
        pe = int(it.page_end or 0)
        if ps and pe and pe >= ps:
            used_pages.update(range(ps, pe + 1))

    # Drop open items only (so re-run is safe after partial filing)
    open_items = (
        db.query(PostItem)
        .filter(
            PostItem.batch_id == batch_id,
            PostItem.status.in_(["inbox", "suggested", "error"]),
        )
        .all()
    )
    for it in open_items:
        try:
            if it.local_path and Path(it.local_path).is_file():
                lp = Path(it.local_path)
                if "splits" in lp.parts:
                    lp.unlink(missing_ok=True)
        except Exception:
            pass
        db.delete(it)
    db.flush()

    if manual:
        ranges = manual
    else:
        ranges = detect_document_page_ranges(
            _normalize_page_texts_with_ink(page_texts, ink) if ink else page_texts,
            ink,
            visual_profiles=profiles,
            split_cues=list_split_cues(db),
        )
    # Clip out pages already filed/emailed
    if used_pages:
        clipped: List[Tuple[int, int, str]] = []
        for ps, pe, reason in ranges:
            segs: List[Tuple[int, int]] = []
            cur_s = None
            for p in range(ps, pe + 1):
                if p in used_pages:
                    if cur_s is not None:
                        segs.append((cur_s, p - 1))
                        cur_s = None
                else:
                    if cur_s is None:
                        cur_s = p
            if cur_s is not None:
                segs.append((cur_s, pe))
            for a, b in segs:
                clipped.append((a, b, reason + " · skip filed pages"))
        ranges = clipped

    n = _build_items_for_batch(
        db,
        batch,
        src,
        page_texts,
        dirs,
        ranges=ranges if ranges else None,
        ink_ratios=ink,
        visual_profiles=profiles,
    )
    batch.page_count = n_pages
    batch.status = "ready"
    batch.processed_at = datetime.utcnow()
    db.commit()

    # OCR + client match for new items
    try:
        for it in (
            db.query(PostItem)
            .filter(
                PostItem.batch_id == batch_id,
                PostItem.status.in_(["inbox", "suggested"]),
            )
            .all()
        ):
            enrich_item_text_and_match(db, it, use_vision=True)
        db.commit()
    except Exception:
        logger.exception("OCR after reprocess batch %s", batch_id)
        try:
            db.rollback()
        except Exception:
            pass

    # Learn keep-together when user forces a single whole-file range
    if manual and len(manual) == 1:
        ps, pe, _ = manual[0]
        if ps == 1 and pe >= (batch.page_count or pe) and pe >= 2:
            try:
                sample = (
                    db.query(PostItem)
                    .filter(PostItem.batch_id == batch_id)
                    .order_by(PostItem.id.desc())
                    .first()
                )
                if sample:
                    learn_keep_together_from_item(db, sample)
            except Exception:
                logger.exception("learn_keep_together failed batch=%s", batch_id)

    mode = "manual ranges" if manual else "auto (blank + letterhead + court keep)"
    return True, f"Re-split into {n} document(s) from {n_pages} page(s) · {mode}"


def split_item(
    db: Session,
    item_id: int,
    breaks: List[int],
) -> Tuple[bool, str]:
    """
    Split a multi-page item after the given page numbers.
    e.g. breaks=[3, 7] on a 10-page doc → items 1-3, 4-7, 8-10.
    """
    item = (
        db.query(PostItem)
        .options(joinedload(PostItem.batch))
        .filter(PostItem.id == item_id)
        .first()
    )
    if not item or not item.batch:
        return False, "Item not found"
    if (item.status or "") in ("filed", "emailed", "dismissed"):
        return False, "Cannot split a completed item"

    src = Path(item.local_path or item.batch.source_path or item.batch.archived_path or "")
    # Prefer original archived batch PDF for full pages
    for candidate in (
        item.batch.archived_path,
        item.batch.source_path,
        item.local_path,
    ):
        if candidate and Path(candidate).is_file() and Path(candidate).suffix.lower() == ".pdf":
            src = Path(candidate)
            break
    if not src.is_file() or src.suffix.lower() != ".pdf":
        return False, "Source PDF not available for split"

    page_count = item.batch.page_count or item.page_end or 0
    if page_count < 2:
        pc, _ = _extract_pdf_text_and_pages(src)
        page_count = pc or 1
    if page_count < 2:
        return False, "Document has only one page"

    cuts = sorted({int(b) for b in breaks if 1 <= int(b) < page_count})
    if not cuts:
        return False, "Provide split points after page numbers (e.g. 3,7)"

    ranges: List[Tuple[int, int]] = []
    start = item.page_start or 1
    # If item already a subset, map breaks within full batch coordinates
    full_end = page_count
    for cut in cuts:
        if cut < start:
            continue
        ranges.append((start, cut))
        start = cut + 1
    ranges.append((start, item.page_end or full_end))

    dirs = ensure_inbox_dirs()
    new_items: List[PostItem] = []
    base_order = item.sort_order or 0

    for idx, (ps, pe) in enumerate(ranges):
        if pe < ps:
            continue
        out = dirs["splits"] / f"batch{item.batch_id}_item{item.id}_{ps}-{pe}.pdf"
        ok, err = _split_pdf_pages(src, out, ps, pe)
        if not ok:
            return False, err or "Split failed"
        # Extract text for this range only when small
        _, text = _extract_pdf_text_and_pages(out)
        rule, action, category, conf = apply_rules(db, text)
        client, cconf, reason = suggest_client(db, text)
        ni = PostItem(
            batch_id=item.batch_id,
            sort_order=base_order + idx,
            title=(text.splitlines()[0][:200] if text.strip() else f"Pages {ps}–{pe}"),
            page_start=ps,
            page_end=pe,
            local_path=str(out),
            content_type="application/pdf",
            size_bytes=int(out.stat().st_size),
            text_excerpt=(text or "")[:8000] or None,
            category=category,
            suggested_action=action,
            suggested_client_id=client.id if client else item.suggested_client_id,
            confidence=max(conf, cconf) if (conf or cconf) else item.confidence,
            match_reason=reason or item.match_reason,
            status="suggested" if action != "review" else "inbox",
            rule_id=rule.id if rule else None,
            created_at=datetime.utcnow(),
        )
        db.add(ni)
        new_items.append(ni)

    # Remove original item
    db.delete(item)
    db.commit()
    return True, f"Split into {len(new_items)} documents"


def _category_for_action(action: str, category: str | None) -> str:
    cat = (category or "").strip()
    if cat in DOCUMENT_CATEGORIES:
        return cat
    if action == "file_hmrc" or cat == "HMRC":
        return "Tax Return" if "Tax Return" in DOCUMENT_CATEGORIES else "Correspondence"
    if cat == "Companies House":
        return "Correspondence"
    if cat == "Chase / demand":
        return "Correspondence"
    return "Correspondence" if "Correspondence" in DOCUMENT_CATEGORIES else "Other"


def _pdf_bytes_for_email_attach(
    path: Path, *, max_bytes: int = 2_800_000
) -> Optional[bytes]:
    """
    Load PDF bytes for email. Graph simple attachments max ~3MB — if larger,
    re-save with PyMuPDF compression / downscale pages.
    """
    path = Path(path)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if len(raw) <= max_bytes:
        return raw
    try:
        import fitz

        src = fitz.open(str(path))
        data = raw
        # try progressively smaller scales until under limit
        for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
            out = fitz.open()
            for i in range(src.page_count):
                page = src[i]
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                rect = fitz.Rect(0, 0, pix.width, pix.height)
                npage = out.new_page(width=pix.width, height=pix.height)
                npage.insert_image(rect, pixmap=pix)
            data = out.tobytes(deflate=True, garbage=3)
            out.close()
            if len(data) <= max_bytes:
                break
        src.close()
        return data
    except Exception as exc:
        logger.warning("PDF shrink for email failed: %s", exc)
        # Fall back to original (Graph may reject if >3MB)
        return raw


def _parse_email_addresses(raw: str) -> List[str]:
    found: List[str] = []
    for part in re.split(r"[,\s;]+", raw or ""):
        addr = part.strip().strip("<>")
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
            low = addr.lower()
            if low not in found:
                found.append(addr)
    return found[:8]


def _post_pdf_attachments(db: Session, item: PostItem, path: Path) -> List[Dict[str, Any]]:
    ensure_item_file(db, item)
    attach_path = Path(item.local_path) if item.local_path else Path()
    if not attach_path.is_file():
        attach_path = path if path.is_file() else Path()
    if not attach_path.is_file():
        return []
    try:
        raw = _pdf_bytes_for_email_attach(attach_path)
    except Exception as exc:
        logger.warning("Could not read post PDF for email: %s", exc)
        return []
    if not raw:
        return []
    safe_name = (
        f"post_{(item.title or attach_path.stem)[:60]}"
        .replace("/", "-")
        .replace("\\", "-")
        + ".pdf"
    )
    return [
        {
            "name": safe_name,
            "content": raw,
            "content_type": "application/pdf",
        }
    ]


def apply_item_action(
    db: Session,
    item_id: int,
    *,
    action: str,
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    category: str = "",
    notes: str = "",
    learn: bool = False,
    learn_keywords: str = "",
    reviewed_by: str = "",
    task_title: str = "",
    task_due: Optional[str] = None,
    task_priority: str = "Medium",
    email_to: str = "",
    email_subject: str = "",
) -> Tuple[bool, str]:
    """File / email / dismiss / create a task from a post item."""
    from app.services import documents as docs_svc
    from app.services import practice_emails as mail_svc

    item = (
        db.query(PostItem)
        .options(joinedload(PostItem.batch))
        .filter(PostItem.id == item_id)
        .first()
    )
    if not item:
        return False, "Item not found"
    if (item.status or "") in ("filed", "emailed", "dismissed"):
        return False, f"Already {item.status}"

    action = (action or "").strip()
    if action not in POST_ACTIONS and action not in (
        "file_client",
        "file_hmrc",
        "email_client",
        "file_and_email",
        "email_other",
        "file_and_email_other",
        "file_email_and_task",
        "file_and_task",
        "create_task",
        "dismiss",
        "delete",
        "review",
    ):
        return False, "Unknown action"

    cid = client_id or item.client_id or item.suggested_client_id
    jid = job_id or item.job_id or item.suggested_job_id
    cat = _category_for_action(action, category or item.category)
    if action in ("file_and_task", "create_task") and not cid:
        prac = find_practice_client(db, item.text_excerpt or "")
        if prac:
            cid = prac.id
    if action == "file_and_task" and not cid:
        return False, "Select a client to file against (Accology for practice mail)"

    if action == "dismiss":
        item.status = "dismissed"
        item.action_taken = "dismiss"
        item.review_notes = (notes or "").strip() or None
        item.reviewed_by = (reviewed_by or "").strip() or None
        item.reviewed_at = datetime.utcnow()
        db.commit()
        return True, "Dismissed"

    if action == "delete":
        # Permanent remove — no client required (tests / junk / bad splits)
        try:
            if item.local_path:
                lp = Path(item.local_path)
                if lp.is_file() and "splits" in lp.parts:
                    lp.unlink(missing_ok=True)
        except Exception:
            pass
        batch = item.batch
        db.delete(item)
        if batch:
            left = (
                db.query(PostItem)
                .filter(PostItem.batch_id == batch.id)
                .count()
            )
            # count after delete needs flush first
            db.flush()
            left = (
                db.query(PostItem)
                .filter(PostItem.batch_id == batch.id)
                .count()
            )
            if left == 0:
                db.delete(batch)
        db.commit()
        return True, "Deleted"

    if action in ("file_client", "file_hmrc", "file_and_email", "email_client"):
        if not cid:
            return False, "Select a client first"
    if action in ("email_other", "file_and_email_other") and not _parse_email_addresses(
        email_to
    ):
        return False, "Enter the email address to send to (not the client)"
    if action in ("file_and_email_other", "file_email_and_task") and not cid:
        prac = find_practice_client(db, item.text_excerpt or "")
        if prac:
            cid = prac.id
        elif action == "file_and_email_other":
            return False, "Select a client to file against (email still goes to the address you typed)"
        else:
            return False, "Select a client to file against"

    path = Path(item.local_path or "")
    if not path.is_file() and item.batch:
        for cand in (item.batch.archived_path, item.batch.source_path):
            if cand and Path(cand).is_file():
                path = Path(cand)
                break
    if action.startswith("file") and not path.is_file():
        return False, "Local file missing — re-import the scan"

    doc = None
    if action in (
        "file_client",
        "file_hmrc",
        "file_and_email",
        "file_and_email_other",
        "file_email_and_task",
        "file_and_task",
    ):
        content = path.read_bytes()
        fname = path.name
        title = (item.title or path.stem)[:240]
        tags = "post-inbox"
        if action == "file_hmrc":
            tags += ",hmrc"
            cat = _category_for_action("file_hmrc", "HMRC")
        doc, err = docs_svc.create_document(
            db,
            filename=fname,
            content=content,
            content_type=item.content_type or "application/pdf",
            title=title,
            description=(notes or item.review_notes or "Filed from post inbox")[:500],
            tags=tags,
            category=cat,
            client_id=cid,
            job_id=jid,
            uploaded_by=(reviewed_by or "post-inbox")[:80],
        )
        if err or not doc:
            return False, err or "Could not file document to OneDrive"
        item.document_id = doc.id
        item.client_id = cid
        item.job_id = jid
        item.category = item.category or cat

    email_msg = ""
    other_addrs = _parse_email_addresses(email_to)
    send_other = action in ("email_other", "file_and_email_other") or (
        action == "file_email_and_task" and bool(other_addrs)
    )
    send_client = action in ("email_client", "file_and_email") or (
        action == "file_email_and_task" and not other_addrs
    )
    if send_other or send_client:
        client = db.query(Client).filter(Client.id == cid).first() if cid else None
        recipients: List[str] = []
        if send_other:
            recipients = other_addrs
        else:
            if not client:
                if action == "file_email_and_task":
                    send_client = False
                    recipients = []
                    email_msg = " (no client to email — filed and task created)"
                else:
                    return False, "Client required to email the client"
            to = (client.email or "").strip()
            if not to:
                for p in client.people or []:
                    if (p.email or "").strip():
                        to = p.email.strip()
                        break
            if not to:
                if action == "file_email_and_task":
                    send_client = False
                    recipients = []
                    email_msg = " (no client email — filed and task created; type an address to email someone else)"
                else:
                    return False, "Client has no email — add one, or use Email this address"
            else:
                recipients = [to]

        log_client_id = cid
        if not log_client_id:
            prac = find_practice_client(db, item.text_excerpt or "")
            log_client_id = prac.id if prac else None
        if not log_client_id:
            return False, "Select a client to log the email against (it still goes to the address you typed)"

        if send_other:
            subject = (
                (email_subject or "").strip()
                or f"{(item.title or 'Correspondence')[:80]}"
            )
            greet = "Hello"
            body = (
                f"{greet},\n\n"
                f"{(notes or '').strip() or 'Please see the attached correspondence.'}\n\n"
                f"Kind regards,\n"
            )
        elif client:
            subject = f"Correspondence received: {(item.title or 'Document')[:80]}"
            body = (
                f"Dear {client.contact_name or client.display_name()},\n\n"
                f"We have received correspondence which appears to relate to your affairs"
                f"{' (' + (item.category or '') + ')' if item.category else ''}.\n\n"
                f"{(notes or '').strip() or 'Please see the attached PDF and let us know if you need us to act.'}\n\n"
                f"Kind regards,\n"
            )
        else:
            subject = (item.title or "Correspondence")[:80]
            body = (notes or "").strip() or "Please see the attached correspondence."
        if doc and doc.onedrive_web_url:
            body += f"\nAlso filed on our system: {doc.onedrive_web_url}\n"

        attachments = _post_pdf_attachments(db, item, path)
        if not attachments:
            body += (
                "\n(Note: the scanned PDF could not be attached from the server.)\n"
            )

        if recipients:
            try:
                cap = mail_svc.send_capability(db)
                if not cap.get("can_send"):
                    if action in ("email_client", "email_other") and not doc:
                        return False, cap.get("graph_error") or "Email not connected"
                    email_msg = " (email skipped — not connected; document filed)"
                else:
                    sent_ok = 0
                    last_flash = ""
                    for addr in recipients:
                        row, flash = mail_svc.send_practice_email(
                            db,
                            client_id=log_client_id,
                            job_id=jid,
                            to_address=addr,
                            subject=subject,
                            body=body,
                            sent_by=reviewed_by or "post-inbox",
                            attachments=attachments,
                        )
                        last_flash = flash or (row.status if row else "")
                        if row and (row.status or "") == "sent":
                            sent_ok += 1
                    who = "the address you typed" if send_other else "client"
                    if sent_ok:
                        email_msg = (
                            f" and emailed {who}"
                            + (" with PDF attached" if attachments else "")
                            + (
                                f" ({sent_ok} message{'s' if sent_ok != 1 else ''})"
                                if sent_ok > 1
                                else ""
                            )
                        )
                    else:
                        email_msg = f" (email: {last_flash or 'failed'})"
            except Exception as exc:
                email_msg = f" (email error: {exc})"

    task_msg = ""
    if action in ("file_and_task", "file_email_and_task", "create_task"):
        try:
            from datetime import date as _date

            from app.services.practice_tasks import create_task as _create_task

            due = None
            raw_due = (task_due or "").strip()
            if raw_due:
                try:
                    due = _date.fromisoformat(raw_due[:10])
                except ValueError:
                    due = None
            title = (task_title or "").strip() or (item.title or "Post to action")[:200]
            desc_bits = [
                f"From scanned post #{item.id}.",
                (item.match_reason or "").strip(),
                (notes or "").strip(),
            ]
            if item.local_path:
                desc_bits.append(f"Review: /post/items/{item.id}")
            task = _create_task(
                db,
                title=title,
                description="\n".join(b for b in desc_bits if b)[:2000],
                client_id=cid,
                job_id=jid,
                notes=(notes or "").strip() or None,
                priority=(task_priority or "Medium").strip() or "Medium",
                due_on=due,
                import_source="post_inbox",
                import_hash=f"post:{item.id}",
                post_item_id=item.id,
                document_id=doc.id if doc else item.document_id,
                commit=False,
            )
            item.task_id = task.id
            task_msg = f" · task #{task.id}"
        except Exception as exc:
            logger.exception("create task from post %s", item.id)
            if action == "create_task":
                return False, f"Could not create task: {exc}"
            task_msg = f" (task failed: {exc})"

    if action == "file_hmrc":
        item.action_taken = "file_hmrc"
        item.status = "filed"
    elif action == "file_client":
        item.action_taken = "file_client"
        item.status = "filed"
    elif action == "file_and_task":
        item.action_taken = "file_and_task"
        item.status = "filed"
    elif action == "file_email_and_task":
        item.action_taken = "file_email_and_task"
        item.status = "filed"
    elif action == "create_task":
        item.action_taken = "create_task"
        # Stay in the inbox so it can still be filed
        item.status = item.status if item.status in ("inbox", "suggested") else "suggested"
    elif action == "email_other":
        item.action_taken = "email_other"
        item.status = "emailed"
        if "skipped" in email_msg or "failed" in email_msg or "error" in email_msg:
            item.status = "error"
            item.review_notes = email_msg
            db.commit()
            return False, email_msg.strip(" ()")
    elif action == "file_and_email_other":
        item.action_taken = "file_and_email_other"
        item.status = "filed"
    elif action == "email_client":
        item.action_taken = "email_client"
        item.status = "emailed" if "emailed" in email_msg or "email failed" not in email_msg else "error"
        if "skipped" in email_msg or "failed" in email_msg or "error" in email_msg:
            if not doc:
                item.status = "error"
                item.review_notes = email_msg
                db.commit()
                return False, email_msg.strip(" ()")
        item.status = "emailed"
    elif action == "file_and_email":
        item.action_taken = "file_and_email"
        item.status = "filed"
    else:
        item.action_taken = action
        item.status = "inbox"

    item.client_id = cid or item.client_id
    item.job_id = jid or item.job_id
    item.review_notes = (notes or "").strip() or item.review_notes
    item.reviewed_by = (reviewed_by or "").strip() or None
    item.reviewed_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()

    if learn and (learn_keywords or item.text_excerpt):
        kws = (learn_keywords or "").strip()
        if not kws and item.text_excerpt:
            # take distinctive words from excerpt
            words = re.findall(r"[A-Za-z][A-Za-z&]{3,}", item.text_excerpt[:500])
            # Prefer uppercase-ish tokens / known brands
            kws = ", ".join(list(dict.fromkeys(words))[:8])
        if kws:
            rule = PostRule(
                name=f"Learned: {(item.title or 'post')[:60]}",
                keywords=kws,
                match_mode="any",
                action=action if action != "file_and_email" else "file_client",
                category=item.category,
                client_id=cid if action != "file_hmrc" else None,
                priority=150,
                is_active=True,
                hit_count=1,
                learned=True,
                notes=f"Learned from post item #{item.id}",
                created_at=datetime.utcnow(),
            )
            db.add(rule)
            db.flush()
            item.rule_id = rule.id

    # Mark batch reviewed if all items done
    batch = item.batch
    if batch:
        open_n = (
            db.query(PostItem)
            .filter(PostItem.batch_id == batch.id)
            .filter(PostItem.status.in_(["inbox", "suggested", "error"]))
            .count()
        )
        if open_n == 0:
            batch.status = "reviewed"

    db.commit()
    msg = f"Done: {item.action_taken}"
    if doc:
        msg += f" · document #{doc.id}"
    msg += email_msg
    msg += task_msg
    if item.task_id and action in ("file_and_task", "create_task"):
        msg += f" · open /tasks/{item.task_id}/edit"
    return True, msg


def inbox_counts(db: Session) -> Dict[str, int]:
    open_n = (
        db.query(PostItem)
        .filter(PostItem.status.in_(["inbox", "suggested", "error"]))
        .count()
    )
    holding_n = db.query(PostItem).filter(PostItem.status == "holding").count()
    return {
        "open": open_n,
        "holding": holding_n,
        "batches": db.query(PostBatch).count(),
        "rules": db.query(PostRule).filter(PostRule.is_active.is_(True)).count(),
    }


def list_open_items(db: Session, *, limit: int = 100) -> List[PostItem]:
    return (
        db.query(PostItem)
        .options(
            joinedload(PostItem.batch),
            joinedload(PostItem.suggested_client),
            joinedload(PostItem.client),
        )
        .filter(PostItem.status.in_(["inbox", "suggested", "error"]))
        .order_by(PostItem.created_at.desc())
        .limit(limit)
        .all()
    )


def list_recent_items(db: Session, *, limit: int = 50) -> List[PostItem]:
    return (
        db.query(PostItem)
        .options(
            joinedload(PostItem.batch),
            joinedload(PostItem.client),
            joinedload(PostItem.suggested_client),
        )
        .order_by(PostItem.updated_at.desc())
        .limit(limit)
        .all()
    )


def get_item(db: Session, item_id: int) -> Optional[PostItem]:
    return (
        db.query(PostItem)
        .options(
            joinedload(PostItem.batch),
            joinedload(PostItem.suggested_client),
            joinedload(PostItem.client),
            joinedload(PostItem.document),
        )
        .filter(PostItem.id == item_id)
        .first()
    )


def item_pdf_page_count(item: PostItem) -> int:
    """Pages in the item's local PDF (fallback to page_end - page_start + 1)."""
    ensure_item_file  # type: ignore  # noqa — ensure available
    path = Path(item.local_path) if item.local_path else None
    if path and path.is_file() and path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            return max(1, len(PdfReader(str(path), strict=False).pages))
        except Exception:
            try:
                import fitz

                doc = fitz.open(str(path))
                n = doc.page_count
                doc.close()
                return max(1, n)
            except Exception:
                pass
    if item.page_start and item.page_end and item.page_end >= item.page_start:
        return int(item.page_end - item.page_start + 1)
    return 1


def list_holding_items(
    db: Session, *, batch_id: Optional[int] = None, limit: int = 100
) -> List[PostItem]:
    q = (
        db.query(PostItem)
        .options(joinedload(PostItem.batch))
        .filter(PostItem.status == "holding")
        .order_by(PostItem.batch_id.asc(), PostItem.page_start.asc(), PostItem.id.asc())
    )
    if batch_id:
        q = q.filter(PostItem.batch_id == batch_id)
    return q.limit(limit).all()


def combine_holding_into_document(
    db: Session,
    holding_ids: List[int],
    *,
    title: Optional[str] = None,
    classify: bool = True,
) -> Tuple[bool, str, Optional[int]]:
    """
    Merge selected holding-area pages into one normal review document
    (status inbox/suggested). Holding rows are deleted after merge.

    Page order follows the order of holding_ids.
    Returns (ok, message, new_item_id).
    """
    ids: List[int] = []
    seen: set = set()
    for raw in holding_ids:
        try:
            hid = int(raw)
        except (TypeError, ValueError):
            continue
        if hid > 0 and hid not in seen:
            seen.add(hid)
            ids.append(hid)
    if not ids:
        return False, "Select at least one holding page to combine", None

    holdings: List[PostItem] = []
    for hid in ids:
        h = (
            db.query(PostItem)
            .options(joinedload(PostItem.batch))
            .filter(PostItem.id == hid, PostItem.status == "holding")
            .first()
        )
        if not h:
            return False, f"Holding page #{hid} not found (already used?)", None
        ensure_item_file(db, h)
        hp = Path(h.local_path or "")
        if not hp.is_file():
            return False, f"PDF missing for holding #{hid}", None
        holdings.append(h)

    # Prefer a common batch; otherwise first item's batch
    batch_id = holdings[0].batch_id
    batch_counts: Dict[int, int] = {}
    for h in holdings:
        if h.batch_id:
            batch_counts[int(h.batch_id)] = batch_counts.get(int(h.batch_id), 0) + 1
    if batch_counts:
        batch_id = max(batch_counts.items(), key=lambda x: x[1])[0]

    try:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        page_starts: List[int] = []
        for h in holdings:
            reader = PdfReader(str(h.local_path), strict=False)
            if not reader.pages:
                return False, f"Holding #{h.id} has no pages", None
            for pg in reader.pages:
                writer.add_page(pg)
            if h.page_start:
                page_starts.append(int(h.page_start))

        n_pages = len(writer.pages)
        if n_pages < 1:
            return False, "Combine produced empty PDF", None

        dirs = ensure_inbox_dirs()
        dest = (
            dirs["splits"]
            / f"holding_combined_b{batch_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S%f')}.pdf"
        )
        with dest.open("wb") as f:
            writer.write(f)
    except Exception as exc:
        return False, f"Combine failed: {exc}", None

    # Light text extract for title/match (OCR enrichment optional after)
    _, text = _extract_pdf_text_and_pages(dest)
    rule, action, category, conf = apply_rules(db, text or "")
    client, cconf, match_reason = suggest_client(db, text or "")
    if rule and rule.client_id and not client:
        client = db.query(Client).filter(Client.id == rule.client_id).first()
        if client:
            match_reason = f"Rule: {rule.name}"
            cconf = max(cconf, 0.7)

    scan_lo = min(page_starts) if page_starts else None
    scan_hi = max(page_starts) if page_starts else None
    # If multi-page holdings contributed, approximate end from total pages
    if scan_lo is not None and n_pages > 1 and (scan_hi is None or scan_hi < scan_lo + n_pages - 1):
        scan_hi = scan_lo + n_pages - 1

    auto_title = (title or "").strip()
    if not auto_title:
        auto_title = _title_from_text(
            text or "",
            f"Combined from holding ({n_pages} page{'s' if n_pages != 1 else ''})",
        )
    if len(holdings) > 1 and not (title or "").strip():
        # Prefer a clear holding-combine label when OCR is weak
        if not text or len(text.strip()) < 40:
            pages_bit = (
                f"scan p{scan_lo}" + (f"–{scan_hi}" if scan_hi and scan_hi != scan_lo else "")
                if scan_lo
                else f"{n_pages} pages"
            )
            auto_title = f"Combined document · {pages_bit}"

    reason_bits = [
        f"Combined from {len(holdings)} holding page(s)",
        f"ids {','.join(str(h.id) for h in holdings)}",
    ]
    if match_reason:
        reason_bits.append(match_reason)

    item = PostItem(
        batch_id=batch_id,
        sort_order=8000,
        title=(auto_title or "Combined document")[:200],
        page_start=scan_lo,
        page_end=scan_hi if scan_hi is not None else scan_lo,
        local_path=str(dest),
        content_type="application/pdf",
        size_bytes=int(dest.stat().st_size) if dest.is_file() else 0,
        text_excerpt=(text or "")[:8000] or None,
        category=category or "Other",
        suggested_action=action or "review",
        suggested_client_id=client.id if client else None,
        confidence=max(conf, cconf) if (conf or cconf) else 0.35,
        match_reason=" · ".join(reason_bits)[:500],
        status="suggested" if (action and action != "review" and client) else "inbox",
        rule_id=rule.id if rule else None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(item)
    db.flush()  # get item.id

    # Delete holding source rows + files
    for h in holdings:
        try:
            hp = Path(h.local_path or "")
            if hp.is_file() and "splits" in hp.parts:
                hp.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tdir = dirs["splits"] / "thumbs"
            for oldt in tdir.glob(f"i{h.id}_*.png"):
                oldt.unlink(missing_ok=True)
        except Exception:
            pass
        db.delete(h)

    db.commit()
    db.refresh(item)

    if classify:
        try:
            enrich_item_text_and_match(db, item, use_vision=True)
            db.commit()
            db.refresh(item)
        except Exception as exc:
            logger.warning("combine classify failed item=%s: %s", item.id, exc)

    msg = (
        f"Combined {len(holdings)} holding page(s) into document #{item.id} "
        f"({n_pages} page{'s' if n_pages != 1 else ''}) — ready to review"
    )
    return True, msg, int(item.id)


def _write_pdf_pages(source: Path, dest: Path, page_indices_0: List[int]) -> Tuple[bool, str]:
    """Write 0-based page indices from source PDF to dest (preserves order)."""
    if not page_indices_0:
        return False, "No pages"
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(source), strict=False)
        n = len(reader.pages)
        writer = PdfWriter()
        for i in page_indices_0:
            if 0 <= i < n:
                writer.add_page(reader.pages[i])
        if len(writer.pages) < 1:
            return False, "No valid pages"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            writer.write(f)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def detach_pages_to_holding(
    db: Session, item_id: int, pages_1based: List[int]
) -> Tuple[bool, str]:
    """
    Remove selected pages (1-based within this item's PDF) into the holding area.
    Each detached page becomes a status=holding PostItem (single-page PDF).
    """
    item = get_item(db, item_id)
    if not item:
        return False, "Item not found"
    if (item.status or "") in ("filed", "emailed", "dismissed"):
        return False, f"Cannot edit pages on {item.status} items"
    if (item.status or "") == "holding":
        return False, "Already in holding — attach to a document instead"

    ensure_item_file(db, item)
    src = Path(item.local_path) if item.local_path else Path()
    if not src.is_file():
        return False, "PDF missing on disk"

    n = item_pdf_page_count(item)
    want = sorted({int(p) for p in pages_1based if 1 <= int(p) <= n})
    if not want:
        return False, "Select at least one valid page number"
    if len(want) >= n:
        return False, "Cannot remove every page — delete the item instead, or leave one page"

    keep = [i for i in range(n) if (i + 1) not in set(want)]
    dirs = ensure_inbox_dirs()
    batch_id = item.batch_id

    # Map item-relative page → original scan page number when known
    def orig_page(rel_1: int) -> int:
        if item.page_start:
            return int(item.page_start) + rel_1 - 1
        return rel_1

    created = 0
    for rel in want:
        dest = dirs["splits"] / f"holding_b{batch_id}_from{item.id}_p{rel}_{datetime.utcnow().strftime('%H%M%S%f')}.pdf"
        ok, err = _write_pdf_pages(src, dest, [rel - 1])
        if not ok:
            return False, f"Could not extract page {rel}: {err}"
        op = orig_page(rel)
        h = PostItem(
            batch_id=batch_id,
            sort_order=9000 + op,
            title=f"Holding · scan p{op}",
            page_start=op,
            page_end=op,
            local_path=str(dest),
            content_type="application/pdf",
            size_bytes=int(dest.stat().st_size) if dest.is_file() else 0,
            category="Other",
            suggested_action="review",
            status="holding",
            match_reason=f"Detached from item #{item.id} (page {rel} of that PDF)",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(h)
        created += 1

    # Rebuild remaining document
    keep_dest = dirs["splits"] / f"batch{batch_id}_item{item.id}_edit.pdf"
    ok, err = _write_pdf_pages(src, keep_dest, keep)
    if not ok:
        db.rollback()
        return False, f"Could not rebuild document: {err}"
    # Remove old path if under splits and different
    old = src
    item.local_path = str(keep_dest)
    item.size_bytes = int(keep_dest.stat().st_size) if keep_dest.is_file() else 0
    # Update page range metadata: keep contiguous only if we removed from ends
    if item.page_start and item.page_end:
        # Approximate: keep original start, shrink by count removed mid-range is imperfect
        # Store remaining count in page_end as start + n_keep - 1 for display
        item.page_end = int(item.page_start) + len(keep) - 1
    item.updated_at = datetime.utcnow()
    item.match_reason = (
        (item.match_reason or "") + f" · removed {len(want)} page(s) to holding"
    ).strip(" ·")
    try:
        if old.is_file() and old.resolve() != keep_dest.resolve() and "splits" in old.parts:
            # only delete if not the batch archive
            if "holding_" not in old.name:
                old.unlink(missing_ok=True)
    except Exception:
        pass

    db.commit()
    return True, f"Moved {created} page(s) to holding · document now has {len(keep)} page(s)"


def attach_holding_to_item(
    db: Session,
    item_id: int,
    holding_ids: List[int],
    *,
    position: str = "end",
    insert_at: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Attach holding-area page PDFs onto a document.
    position: "start" | "end" (used when insert_at is None).
    insert_at: 0-based index among *current* pages before which to insert
               (0 = start, n = end). Takes precedence over position.
    Holding items are deleted after merge.
    """
    # Build sequence tokens then use apply_page_sequence
    item = get_item(db, item_id)
    if not item:
        return False, "Document not found"
    if (item.status or "") in ("filed", "emailed", "dismissed", "holding"):
        return False, "Open a normal document to attach pages (not a holding item)"

    n = item_pdf_page_count(item)
    base_tokens = [f"p{i}" for i in range(1, n + 1)]
    hold_tokens = [f"h{int(hid)}" for hid in holding_ids if int(hid) > 0]
    if not hold_tokens:
        return False, "Select holding page(s) to attach"

    if insert_at is not None:
        idx = max(0, min(int(insert_at), n))
        seq = base_tokens[:idx] + hold_tokens + base_tokens[idx:]
    elif (position or "end").lower() == "start":
        seq = hold_tokens + base_tokens
    else:
        seq = base_tokens + hold_tokens

    return apply_page_sequence(db, item_id, seq)


def item_thumbnail_png(
    db: Session,
    item: PostItem,
    *,
    page: int = 1,
    max_width: int = 220,
) -> Optional[bytes]:
    """
    Render a medium PNG thumbnail for a page of the item's PDF (1-based page).
    Cached under splits/thumbs/.
    """
    ensure_item_file(db, item)
    path = Path(item.local_path) if item.local_path else Path()
    if not path.is_file():
        return None

    page_1 = max(1, int(page or 1))
    try:
        mtime = int(path.stat().st_mtime)
        size = int(path.stat().st_size)
    except Exception:
        mtime, size = 0, 0

    dirs = ensure_inbox_dirs()
    thumb_dir = dirs["splits"] / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    cache = thumb_dir / f"i{item.id}_p{page_1}_w{max_width}_{mtime}_{size}.png"
    if cache.is_file() and cache.stat().st_size > 40:
        try:
            return cache.read_bytes()
        except Exception:
            pass

    # Invalidate older thumbs for this item/page
    try:
        for old in thumb_dir.glob(f"i{item.id}_p{page_1}_w{max_width}_*.png"):
            if old != cache:
                old.unlink(missing_ok=True)
    except Exception:
        pass

    # Image files
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff"):
        try:
            from PIL import Image
            import io

            im = Image.open(path)
            im.thumbnail((max_width, int(max_width * 1.45)), Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            cache.write_bytes(data)
            return data
        except Exception as exc:
            logger.warning("PIL thumb failed: %s", exc)
            return None

    if path.suffix.lower() != ".pdf":
        return None

    try:
        import fitz

        doc = fitz.open(str(path))
        if page_1 > doc.page_count:
            doc.close()
            return None
        pg = doc[page_1 - 1]
        # Aim for ~max_width CSS pixels at 96dpi
        zoom = max(0.35, min(2.0, max_width / max(pg.rect.width, 1.0)))
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("png")
        doc.close()
        try:
            cache.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception as exc:
        logger.warning("thumb render failed item=%s page=%s: %s", item.id, page_1, exc)
        return None


def reorder_item_pages(
    db: Session, item_id: int, pages_1based: List[int]
) -> Tuple[bool, str]:
    """Reorder pages of a document PDF (1-based indices, each page once)."""
    item = get_item(db, item_id)
    if not item:
        return False, "Item not found"
    n = item_pdf_page_count(item)
    if n < 1:
        return False, "No pages"
    want = [int(p) for p in pages_1based if 1 <= int(p) <= n]
    if len(want) != n or len(set(want)) != n:
        return False, f"Order must list each of the {n} page(s) exactly once"
    seq = [f"p{p}" for p in want]
    return apply_page_sequence(db, item_id, seq)


def apply_page_sequence(
    db: Session, item_id: int, sequence: List[str]
) -> Tuple[bool, str]:
    """
    Rebuild a document from a page sequence.

    Tokens:
      p{n}  — page n (1-based) of the *current* document PDF
      h{id} — holding PostItem id (all pages of that holding PDF, usually 1)

    All current pages must appear exactly once (so nothing is lost).
    Holding pages listed are merged in and removed from the holding area.
    """
    item = get_item(db, item_id)
    if not item:
        return False, "Document not found"
    if (item.status or "") in ("filed", "emailed", "dismissed"):
        return False, f"Cannot reorder {item.status} items"
    if (item.status or "") == "holding":
        return False, "Cannot apply sequence to a holding page — open the target document"

    ensure_item_file(db, item)
    base = Path(item.local_path) if item.local_path else Path()
    if not base.is_file():
        return False, "Document PDF missing on disk"

    n = item_pdf_page_count(item)
    tokens: List[str] = []
    for raw in sequence:
        t = (raw or "").strip().lower()
        if not t:
            continue
        if t.startswith("p") and t[1:].isdigit():
            tokens.append(f"p{int(t[1:])}")
        elif t.startswith("h") and t[1:].isdigit():
            tokens.append(f"h{int(t[1:])}")
        elif t.isdigit():
            tokens.append(f"p{int(t)}")
        else:
            return False, f"Bad sequence token: {raw}"

    if not tokens:
        return False, "Empty page order"

    # Validate current pages: each 1..n exactly once
    used_pages = [int(t[1:]) for t in tokens if t.startswith("p")]
    if sorted(used_pages) != list(range(1, n + 1)):
        missing = set(range(1, n + 1)) - set(used_pages)
        extra = [p for p in used_pages if p < 1 or p > n]
        dup = [p for p in used_pages if used_pages.count(p) > 1]
        bits = []
        if missing:
            bits.append(f"missing pages {sorted(missing)}")
        if extra:
            bits.append(f"invalid pages {extra}")
        if dup:
            bits.append(f"duplicate pages {sorted(set(dup))}")
        return False, "Order must include every current page once — " + "; ".join(bits)

    hold_ids = [int(t[1:]) for t in tokens if t.startswith("h")]
    holdings_by_id: Dict[int, PostItem] = {}
    for hid in hold_ids:
        if hid in holdings_by_id:
            continue
        h = (
            db.query(PostItem)
            .filter(PostItem.id == hid, PostItem.status == "holding")
            .first()
        )
        if not h:
            return False, f"Holding page #{hid} not found (already attached?)"
        ensure_item_file(db, h)
        if not h.local_path or not Path(h.local_path).is_file():
            return False, f"Holding file missing for #{hid}"
        holdings_by_id[hid] = h

    try:
        from pypdf import PdfReader, PdfWriter

        base_reader = PdfReader(str(base), strict=False)
        hold_readers: Dict[int, Any] = {}
        for hid, h in holdings_by_id.items():
            hold_readers[hid] = PdfReader(str(h.local_path), strict=False)

        writer = PdfWriter()
        for t in tokens:
            if t.startswith("p"):
                pi = int(t[1:]) - 1
                if 0 <= pi < len(base_reader.pages):
                    writer.add_page(base_reader.pages[pi])
            else:
                hid = int(t[1:])
                for pg in hold_readers[hid].pages:
                    writer.add_page(pg)

        if len(writer.pages) < 1:
            return False, "Rebuild produced empty PDF"

        dirs = ensure_inbox_dirs()
        dest = (
            dirs["splits"]
            / f"batch{item.batch_id}_item{item.id}_seq_{datetime.utcnow().strftime('%H%M%S%f')}.pdf"
        )
        with dest.open("wb") as f:
            writer.write(f)
        n_new = len(writer.pages)
    except Exception as exc:
        return False, f"Rebuild failed: {exc}"

    old = base
    item.local_path = str(dest)
    item.size_bytes = int(dest.stat().st_size) if dest.is_file() else 0
    if item.page_start:
        item.page_end = int(item.page_start) + n_new - 1
    item.updated_at = datetime.utcnow()
    n_hold = len(holdings_by_id)
    note_bits = []
    if n_hold:
        note_bits.append(f"attached {n_hold} holding page(s)")
    if used_pages != list(range(1, n + 1)):
        note_bits.append("reordered pages")
    if note_bits:
        item.match_reason = (
            (item.match_reason or "") + " · " + " · ".join(note_bits)
        ).strip(" ·")

    for hid, h in holdings_by_id.items():
        try:
            hp = Path(h.local_path or "")
            if hp.is_file() and "splits" in hp.parts:
                hp.unlink(missing_ok=True)
        except Exception:
            pass
        # Drop cached thumbs
        try:
            tdir = ensure_inbox_dirs()["splits"] / "thumbs"
            for oldt in tdir.glob(f"i{hid}_*.png"):
                oldt.unlink(missing_ok=True)
        except Exception:
            pass
        db.delete(h)

    try:
        if old.is_file() and old.resolve() != dest.resolve() and "splits" in old.parts:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    # Invalidate doc thumbs
    try:
        tdir = ensure_inbox_dirs()["splits"] / "thumbs"
        for oldt in tdir.glob(f"i{item.id}_*.png"):
            oldt.unlink(missing_ok=True)
    except Exception:
        pass

    db.commit()
    msg = f"Document now has {n_new} page(s)"
    if n_hold:
        msg = f"Merged {n_hold} holding page(s) · " + msg
    if used_pages != list(range(1, n + 1)):
        msg = "Pages reordered · " + msg
    return True, msg
