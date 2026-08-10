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
    r"\bSelf\s+Assessment\b",
    r"\bCorporation\s+Tax\b",
    r"\bPAYE\b",
    r"\bVAT\s+(?:Return|Notice|Statement)\b",
    r"\bForm\s+(?:SA\d+|CT\d+|P\d+|VAT\d+)\b",
    r"\bDear\s+(?:Sir|Madam|Sir/Madam|Mr|Mrs|Ms|Miss)\b",
    r"\bFinal\s+(?:demand|notice|reminder)\b",
    r"\bLetter\s+before\s+action\b",
    r"\bStatutory\s+demand\b",
    r"\bInvoice\s+(?:No\.?|Number|#)\b",
    r"\bTax\s+Invoice\b",
    r"\bStatement\s+of\s+Account\b",
    r"\bReminder\s+notice\b",
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
) -> List[Tuple[int, int, str]]:
    """
    Split a multi-page PDF into documents.

    Returns list of (page_start, page_end, reason) 1-based inclusive.
    Uses:
      - blank separator pages (text and/or low ink on image scans)
      - letterhead / layout change (visual fingerprint) for image-only scans
      - letter/form start markers in OCR text when available
    """
    n = len(page_texts)
    if n == 0:
        return []
    if n == 1:
        return [(1, 1, "single page")]

    profiles = visual_profiles or []
    ink = ink_ratios or [p.get("ink", 0.5) for p in profiles]

    def blank(i: int) -> bool:
        t = page_texts[i] if i < len(page_texts) else ""
        ik = ink[i] if i < len(ink) else None
        return _page_is_blank(t, ik)

    # Build starts: page indices (0-based) that begin a document
    starts: List[int] = [0]
    reasons: Dict[int, str] = {0: "start of file"}

    for i in range(1, n):
        t = page_texts[i] or ""
        prev = page_texts[i - 1] or ""

        # Blank page after content → next non-blank starts new doc
        if blank(i):
            continue

        if blank(i - 1) and not blank(i):
            if i not in starts:
                starts.append(i)
                reasons[i] = "after blank separator"
            continue

        # Image-only: letterhead / layout change (same letterhead ⇒ keep together)
        if i < len(profiles) and i - 1 < len(profiles):
            cur, prev_p = profiles[i], profiles[i - 1]
            top_s = _visual_sim(cur.get("top") or [], prev_p.get("top") or [])
            full_s = _visual_sim(cur.get("full") or [], prev_p.get("full") or [])
            # Strong same letterhead band → continuation of multi-page letter
            if top_s >= 0.82:
                pass  # do not split on layout alone
            elif top_s < 0.76 and full_s < 0.86:
                if i not in starts:
                    starts.append(i)
                    reasons[i] = f"letterhead change (top={top_s:.2f})"
                continue

        if _page_looks_like_new_document(t, is_first=False):
            head = t[:500]
            page1 = bool(re.search(r"\bPage\s*1\s*(?:of|/)\s*\d+\b", head, re.I))
            strong_new = bool(
                re.search(
                    r"\bDear\s+(?:Sir|Madam|Sir/Madam|Mr|Mrs|Ms|Miss)\b"
                    r"|\bFinal\s+(?:demand|notice)\b"
                    r"|\bInvoice\s+(?:No\.?|Number)\b"
                    r"|\bForm\s+(?:SA\d+|CT\d+|P\d+)\b",
                    head,
                    re.I,
                )
            )
            prev_cont = bool(
                re.search(r"\bPage\s*[2-9]\d*\s*(?:of|/)\s*\d+\b", prev[:400], re.I)
            )
            if prev_cont and not page1 and not strong_new:
                continue
            if page1 or strong_new or blank(i - 1):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "letter/form marker" if not page1 else "page 1 of N"
            elif not prev_cont and re.search(
                r"\bHM\s*Revenue|\bHMRC\b|\bCompanies\s+House\b", head, re.I
            ):
                if i not in starts:
                    starts.append(i)
                    reasons[i] = "new letterhead text"

    starts = sorted(set(starts))
    ranges: List[Tuple[int, int, str]] = []
    for idx, st in enumerate(starts):
        while st < n and blank(st):
            st += 1
        if st >= n:
            continue
        if idx + 1 < len(starts):
            en = starts[idx + 1] - 1
        else:
            en = n - 1
        while en > st and blank(en):
            en -= 1
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
        ranges = (
            detect_document_page_ranges(
                page_texts, ink_ratios, visual_profiles=profiles
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
    """Install starter rules if none exist."""
    n = db.query(PostRule).count()
    if n:
        return 0
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
    db.commit()
    return len(starters)


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


def import_from_inbox(db: Session, *, limit: int = 40) -> Dict[str, Any]:
    """
    Import new files from inbox/ into post_batches + post_items.

    Multi-page PDFs are auto-split into multiple items using content markers
    (letterheads, Dear…, Form SA…, blank separators) — same approach as the
    Ahmed Bros multi-invoice pipeline, adapted for post.
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

    mode = "manual ranges" if manual else "auto (blank + letterhead)"
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
) -> Tuple[bool, str]:
    """File / email / dismiss a post item; optionally learn a rule."""
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
        "dismiss",
        "delete",
        "review",
    ):
        return False, "Unknown action"

    cid = client_id or item.client_id or item.suggested_client_id
    jid = job_id or item.job_id or item.suggested_job_id
    cat = _category_for_action(action, category or item.category)

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

    path = Path(item.local_path or "")
    if not path.is_file() and item.batch:
        for cand in (item.batch.archived_path, item.batch.source_path):
            if cand and Path(cand).is_file():
                path = Path(cand)
                break
    if action.startswith("file") and not path.is_file():
        return False, "Local file missing — re-import the scan"

    doc = None
    if action in ("file_client", "file_hmrc", "file_and_email"):
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
    if action in ("email_client", "file_and_email"):
        client = db.query(Client).filter(Client.id == cid).first() if cid else None
        if not client:
            return False, "Client required to email"
        to = (client.email or "").strip()
        if not to:
            # try primary person
            for p in client.people or []:
                if (p.email or "").strip():
                    to = p.email.strip()
                    break
        if not to:
            return False, "Client has no email — add one or file only"
        subject = f"Correspondence received: {(item.title or 'Document')[:80]}"
        body = (
            f"Dear {client.contact_name or client.display_name()},\n\n"
            f"We have received correspondence which appears to relate to your affairs"
            f"{' (' + (item.category or '') + ')' if item.category else ''}.\n\n"
            f"{(notes or '').strip() or 'Please see the attached PDF and let us know if you need us to act.'}\n\n"
            f"Kind regards,\n"
        )
        if doc and doc.onedrive_web_url:
            body += f"\nAlso filed on our system: {doc.onedrive_web_url}\n"

        # Always try to attach the scan PDF (split item file preferred)
        ensure_item_file(db, item)
        attach_path = Path(item.local_path) if item.local_path else Path()
        if not attach_path.is_file():
            attach_path = path if path.is_file() else Path()
        attachments = []
        if attach_path.is_file():
            try:
                raw = _pdf_bytes_for_email_attach(attach_path)
                safe_name = (
                    f"post_{(item.title or attach_path.stem)[:60]}"
                    .replace("/", "-")
                    .replace("\\", "-")
                    + ".pdf"
                )
                if raw:
                    attachments.append(
                        {
                            "name": safe_name,
                            "content": raw,
                            "content_type": "application/pdf",
                        }
                    )
            except Exception as exc:
                logger.warning("Could not read post PDF for email: %s", exc)
        if not attachments:
            body += (
                "\n(Note: the scanned PDF could not be attached from the server. "
                "Please contact us if you need a copy.)\n"
            )

        try:
            cap = mail_svc.send_capability(db)
            if not cap.get("can_send"):
                if action == "email_client" and not doc:
                    return False, cap.get("graph_error") or "Email not connected"
                email_msg = " (email skipped — not connected; document filed)"
            else:
                row, flash = mail_svc.send_practice_email(
                    db,
                    client_id=cid,
                    job_id=jid,
                    to_address=to,
                    subject=subject,
                    body=body,
                    sent_by=reviewed_by or "post-inbox",
                    attachments=attachments,
                )
                if row and (row.status or "") == "sent":
                    email_msg = (
                        " and emailed client with PDF attached"
                        if attachments
                        else " and emailed client"
                    )
                else:
                    email_msg = f" (email: {flash or row.status if row else 'failed'})"
        except Exception as exc:
            email_msg = f" (email error: {exc})"

    if action == "file_hmrc":
        item.action_taken = "file_hmrc"
        item.status = "filed"
    elif action == "file_client":
        item.action_taken = "file_client"
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
    return True, msg


def inbox_counts(db: Session) -> Dict[str, int]:
    open_n = (
        db.query(PostItem)
        .filter(PostItem.status.in_(["inbox", "suggested", "error"]))
        .count()
    )
    return {
        "open": open_n,
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
