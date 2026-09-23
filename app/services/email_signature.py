"""Accology Limited HTML email signature helper (HTTPS Imagine lockup + Powered by badge)."""
from __future__ import annotations

LOCKUP_URL = "https://accology.co/static/branding/logo_lockup_sig.png"
LOCKUP_FALLBACK_URL = "https://accology.co/static/branding/logo_lockup.png"
BADGE_URL = "https://accology.co/static/branding/powered_by/powered_by_accology.png"

SIGNATURE_BY_MAILBOX = {
    "simon@accology.co": {"name": "Simon Duckworth", "title": "Director", "email": "simon@accology.co", "mobile": "07857 224801", "tel": "01204 238938"},
    "accounts@accology.co": {"name": "Accology Accounts", "title": "Accounts", "email": "accounts@accology.co", "mobile": "", "tel": "01204 238938"},
    "marketing@accology.co": {"name": "Accology Marketing", "title": "Marketing", "email": "marketing@accology.co", "mobile": "", "tel": "01204 238938"},
    "tech@accology.co": {"name": "Accology Tech", "title": "Technology", "email": "tech@accology.co", "mobile": "", "tel": "01204 238938"},
}

def limited_signature_html(*, from_mailbox: str = "simon@accology.co", lockup_url: str = LOCKUP_FALLBACK_URL, badge_url: str = BADGE_URL) -> str:
    key = (from_mailbox or "").strip().lower()
    meta = SIGNATURE_BY_MAILBOX.get(key) or SIGNATURE_BY_MAILBOX["simon@accology.co"]
    bits = [f'<a href="mailto:{meta["email"]}" style="color:#0F172A;text-decoration:none;">{meta["email"]}</a>']
    if meta.get("mobile"):
        bits.append(f"Mobile: {meta['mobile']}")
    if meta.get("tel"):
        bits.append(f"Tel: {meta['tel']}")
    contact = " &nbsp;|&nbsp; ".join(bits)
    return (
        '<div class="ac-sig" style="font-family:Inter, Segoe UI, Arial, Helvetica, sans-serif; color:#0F172A; font-size:13px; line-height:1.45; margin-top:16px;">'
        f'<p style="margin:0 0 10px 0;"><a href="https://accology.co" style="text-decoration:none;border:0;">'
        f'<img src="{lockup_url}" width="220" height="66" alt="Accology - Accounting Science" style="display:block;border:0;"></a></p>'
        f'<p style="margin:0 0 2px 0;font-size:14px;font-weight:700;color:#0F172A;">{meta["name"]}</p>'
        f'<p style="margin:0 0 8px 0;font-size:12px;font-weight:600;color:#1E4064;">{meta["title"]}</p>'
        f'<p style="margin:0;font-size:12px;color:#0F172A;">{contact}</p>'
        '<p style="margin:0 0 10px 0;font-size:12px;color:#0F172A;">Fax: 01204 238939 &nbsp;|&nbsp; '
        '<a href="https://accology.co" style="color:#00C8DC;text-decoration:none;">accology.co</a></p>'
        '<p style="margin:0 0 2px 0;font-size:10px;color:#64748B;">Accology is a trading name of Accology Limited - Company number 07210650</p>'
        '<p style="margin:0 0 2px 0;font-size:10px;color:#64748B;">Registered office - Bolton Arena, Arena Approach, Horwich, Bolton, BL6 6LB</p>'
        '<p style="margin:0 0 2px 0;font-size:10px;color:#64748B;">Accology Limited is authorised by The Association of Certified Accountants - Registered number 2143851</p>'
        '<p style="margin:0 0 12px 0;font-size:10px;color:#64748B;">VAT No. 203313763</p>'
        f'<p style="margin:0;"><a href="https://accology.co" style="text-decoration:none;border:0;">'
        f'<img src="{badge_url}" width="166" height="49" alt="Powered by Accology" style="display:block;border:0;"></a></p>'
        "</div>"
    )

def append_limited_signature(body_html: str, *, from_mailbox: str = "simon@accology.co") -> str:
    body = (body_html or "").strip()
    sig = limited_signature_html(from_mailbox=from_mailbox)
    low = body.lower()
    if "powered_by/powered_by_accology.png" in low or "powered by accology" in low:
        return body
    idx = low.rfind("</body>")
    if idx != -1:
        return body[:idx] + "\n" + sig + "\n" + body[idx:]
    return body + "\n" + sig
