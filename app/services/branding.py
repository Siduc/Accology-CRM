"""
Practice branding assets (logo + letterhead) for invoices and letter templates.

Files live under app/static/branding/ (served at /static/branding/…):
  logo.png|jpg|webp     — web invoices, on-screen letterhead mark
  letterhead.pdf        — full Accology letterhead for PDF templates
  letterhead.png        — optional image of the header band (or full page) for HTML/print
  letterhead.docx       — Word letterhead (open from CRM)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import UploadFile

BRANDING_DIR = Path(__file__).resolve().parent.parent / "static" / "branding"
LOGO_STEM = "logo"
LETTERHEAD_PDF = "letterhead.pdf"
LETTERHEAD_DOCX = "letterhead.docx"
LETTERHEAD_IMG_STEM = "letterhead"

_LOGO_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_LETTERHEAD_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_BYTES = 12 * 1024 * 1024  # 12 MB


def ensure_branding_dir() -> Path:
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    return BRANDING_DIR


def _find_stem(stem: str, allowed: set[str]) -> Optional[Path]:
    ensure_branding_dir()
    for p in sorted(BRANDING_DIR.iterdir()) if BRANDING_DIR.is_dir() else []:
        if p.is_file() and p.stem.lower() == stem.lower() and p.suffix.lower() in allowed:
            return p
    return None


def logo_path() -> Optional[Path]:
    return _find_stem(LOGO_STEM, _LOGO_EXT)


def letterhead_pdf_path() -> Optional[Path]:
    p = ensure_branding_dir() / LETTERHEAD_PDF
    return p if p.is_file() else None


def letterhead_docx_path() -> Optional[Path]:
    p = ensure_branding_dir() / LETTERHEAD_DOCX
    return p if p.is_file() else None


def letterhead_image_path() -> Optional[Path]:
    return _find_stem(LETTERHEAD_IMG_STEM, _LETTERHEAD_IMG_EXT)


def pays_letterhead_path() -> Optional[Path]:
    """Imagine lockup with Accology Pays contact — does not replace Accology Limited letterhead."""
    return ensure_pays_letterhead()


def ensure_pays_letterhead() -> Optional[Path]:
    """
    Build letterhead_pays_header.png from the Imagine ACCOLOGY header
    (wordmark + cyan→purple bar). Accology Limited assets are left untouched.
    """
    dest = ensure_branding_dir() / "letterhead_pays_header.png"
    src = letterhead_header_source()
    if not src or not src.is_file():
        return dest if dest.is_file() else None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return dest if dest.is_file() else None

    base = Image.open(src).convert("RGB")
    w, h = base.size
    # Find the thin cyan rule and keep everything above it (Imagine lockup).
    pixels = base.load()
    crop_h = int(h * 0.55)
    for y in range(int(h * 0.35), h - 8):
        cyan_run = 0
        for x in range(0, w, 8):
            r, g, b = pixels[x, y][:3]
            if g > 160 and b > 180 and r < 120:
                cyan_run += 1
        if cyan_run > (w / 8) * 0.45:
            crop_h = min(h, y + 4)
            break
    lockup = base.crop((0, 0, w, crop_h))
    extra = max(72, int(h * 0.28))
    out = Image.new("RGB", (w, lockup.height + extra), (255, 255, 255))
    out.paste(lockup, (0, 0))
    draw = ImageDraw.Draw(out)
    font = None
    for fp in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ):
        if Path(fp).is_file():
            try:
                font = ImageFont.truetype(fp, max(20, int(w / 72)))
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    contact = (
        "Accology Pays Limited  ·  Company 16011017  ·  "
        "Bolton Arena, Bolton BL6 6LB  ·  payroll@accology.co  ·  07857 224801"
    )
    draw.text((8, lockup.height + 10), contact, fill=(100, 110, 120), font=font)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest, "PNG", optimize=True)
    return dest


def letterhead_header_source() -> Optional[Path]:
    """Prefer the Imagine header band; fall back to full letterhead image."""
    for name in ("letterhead_header.png", "letterhead.png"):
        p = ensure_branding_dir() / name
        if p.is_file():
            return p
    return letterhead_image_path()


def static_url(path: Optional[Path]) -> str:
    if not path or not path.is_file():
        return ""
    # Cache-bust with mtime so browser refreshes after re-upload
    try:
        v = int(path.stat().st_mtime)
    except OSError:
        v = 0
    rel = path.name
    return f"/static/branding/{rel}?v={v}"


def branding_status() -> dict:
    logo = logo_path()
    lh_pdf = letterhead_pdf_path()
    lh_docx = letterhead_docx_path()
    lh_img = letterhead_image_path()
    pays_img = pays_letterhead_path()
    dark = BRANDING_DIR / "logo_on_dark.png"
    return {
        "logo_ready": bool(logo),
        "logo_url": static_url(logo),
        "logo_name": logo.name if logo else "",
        "logo_on_dark_url": static_url(dark) if dark.is_file() else static_url(logo),
        "letterhead_pdf_ready": bool(lh_pdf),
        "letterhead_pdf_url": static_url(lh_pdf),
        "letterhead_pdf_name": lh_pdf.name if lh_pdf else "",
        "letterhead_docx_ready": bool(lh_docx),
        "letterhead_docx_url": "/documents/letterhead.docx" if lh_docx else "",
        "letterhead_docx_name": lh_docx.name if lh_docx else "",
        "letterhead_image_ready": bool(lh_img),
        "letterhead_image_url": static_url(lh_img),
        "letterhead_image_name": lh_img.name if lh_img else "",
        "pays_letterhead_ready": bool(pays_img),
        "pays_letterhead_url": static_url(pays_img),
        "any_ready": bool(logo or lh_pdf or lh_docx or lh_img),
    }


def _safe_ext(filename: str, allowed: set[str]) -> Optional[str]:
    name = (filename or "").strip().lower()
    if not name or "." not in name:
        return None
    ext = "." + name.rsplit(".", 1)[-1]
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in allowed and ext != ".pdf":
        return None
    return ext


async def save_upload(
    file: UploadFile,
    *,
    kind: str,
) -> tuple[bool, str]:
    """
    kind: 'logo' | 'letterhead_pdf' | 'letterhead_image'
    Returns (ok, message).
    """
    ensure_branding_dir()
    raw_name = file.filename or ""
    content = await file.read()
    if not content:
        return False, "Empty file"
    if len(content) > _MAX_BYTES:
        return False, "File too large (max 12 MB)"

    kind = (kind or "").strip().lower()
    if kind == "logo":
        ext = _safe_ext(raw_name, _LOGO_EXT)
        if not ext or ext == ".pdf":
            return False, "Logo must be PNG, JPG, or WebP (not PDF)"
        # Remove previous logo.* variants
        for p in list(BRANDING_DIR.glob("logo.*")):
            try:
                p.unlink()
            except OSError:
                pass
        dest = BRANDING_DIR / f"logo{ext}"
        dest.write_bytes(content)
        # Also normalise common jpeg name
        return True, f"Logo saved as {dest.name}"

    if kind == "letterhead_pdf":
        ext = _safe_ext(raw_name, {".pdf"})
        # Allow by content-type / name even if extension helper is strict
        name_l = raw_name.lower()
        ct = (file.content_type or "").lower()
        if not (name_l.endswith(".pdf") or "pdf" in ct or ext == ".pdf"):
            return False, "Letterhead must be a PDF file"
        # Basic PDF magic
        if not content[:5].startswith(b"%PDF"):
            return False, "File does not look like a valid PDF"
        dest = BRANDING_DIR / LETTERHEAD_PDF
        dest.write_bytes(content)
        return True, f"Letterhead PDF saved as {dest.name}"

    if kind == "letterhead_image":
        ext = _safe_ext(raw_name, _LETTERHEAD_IMG_EXT)
        if not ext or ext == ".pdf":
            return False, "Letterhead image must be PNG, JPG, or WebP"
        for p in list(BRANDING_DIR.glob("letterhead.*")):
            if p.suffix.lower() != ".pdf":
                try:
                    p.unlink()
                except OSError:
                    pass
        dest = BRANDING_DIR / f"letterhead{ext}"
        dest.write_bytes(content)
        return True, f"Letterhead image saved as {dest.name}"

    return False, "Unknown upload type"


def delete_asset(kind: str) -> tuple[bool, str]:
    kind = (kind or "").strip().lower()
    ensure_branding_dir()
    removed = []
    if kind == "logo":
        for p in BRANDING_DIR.glob("logo.*"):
            p.unlink(missing_ok=True)
            removed.append(p.name)
    elif kind == "letterhead_pdf":
        p = BRANDING_DIR / LETTERHEAD_PDF
        if p.is_file():
            p.unlink()
            removed.append(p.name)
    elif kind == "letterhead_image":
        for p in BRANDING_DIR.glob("letterhead.*"):
            if p.suffix.lower() != ".pdf":
                p.unlink(missing_ok=True)
                removed.append(p.name)
    else:
        return False, "Unknown type"
    if not removed:
        return False, "Nothing to remove"
    return True, "Removed " + ", ".join(removed)


def practice_branding_context() -> dict:
    """Template context for invoice / letter views."""
    from app.config import PRACTICE_EMAIL, PRACTICE_NAME, PRACTICE_PHONE

    st = branding_status()
    return {
        "practice_name": PRACTICE_NAME,
        "practice_email": PRACTICE_EMAIL or "",
        "practice_phone": PRACTICE_PHONE or "",
        "practice_logo_url": st["logo_url"] or "/static/branding/logo.png",
        "practice_logo_ready": st["logo_ready"],
        "practice_logo_on_dark_url": st.get("logo_on_dark_url")
        or "/static/branding/logo_on_dark.png",
        "practice_letterhead_pdf_url": st["letterhead_pdf_url"],
        "practice_letterhead_pdf_ready": st["letterhead_pdf_ready"],
        "practice_letterhead_image_url": st["letterhead_image_url"],
        "practice_letterhead_image_ready": st["letterhead_image_ready"],
        "pays_letterhead_url": st.get("pays_letterhead_url") or "",
        "pays_letterhead_ready": st.get("pays_letterhead_ready") or False,
    }
