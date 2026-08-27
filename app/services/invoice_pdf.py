"""PDF copy of a CRM sales invoice for emailing to the client."""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from app.models import Client, Job
from app.models.sales import Invoice
from app.services.branding import letterhead_image_path, logo_path
from app.services.sales_ledger import format_uk_date


def _money(value) -> str:
    try:
        return f"£{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "£0.00"


def _line_label(ln, job: Optional[Job]) -> str:
    desc = (getattr(ln, "description", None) or "").strip()
    if desc:
        return desc
    svc = getattr(ln, "service", None)
    if svc and (svc.name or "").strip():
        return svc.name.strip()
    if job and (job.type or "").strip():
        return job.type.strip()
    return "Fee"


def build_invoice_pdf_bytes(
    invoice: Invoice,
    client: Optional[Client] = None,
    job: Optional[Job] = None,
) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    from app.config import PRACTICE_EMAIL, PRACTICE_NAME, PRACTICE_PHONE

    buf = BytesIO()
    cnv = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    left = 18 * mm
    right = page_w - 18 * mm
    navy = HexColor("#0F172A")
    muted = HexColor("#64748B")
    cyan = HexColor("#00E5FF")
    line_grey = HexColor("#E2E8F0")

    y = page_h - 14 * mm
    header = letterhead_image_path()
    if header and header.is_file():
        img_w = right - left
        img_h = 28 * mm
        cnv.drawImage(
            str(header),
            left,
            y - img_h,
            width=img_w,
            height=img_h,
            preserveAspectRatio=True,
            mask="auto",
            anchor="sw",
        )
        y = y - img_h - 8 * mm
    else:
        logo = logo_path()
        if logo and logo.is_file():
            cnv.drawImage(
                str(logo),
                left,
                y - 16 * mm,
                width=28 * mm,
                height=16 * mm,
                preserveAspectRatio=True,
                mask="auto",
                anchor="sw",
            )
        cnv.setFillColor(navy)
        cnv.setFont("Helvetica-Bold", 12)
        cnv.drawString(left + 32 * mm, y - 6 * mm, PRACTICE_NAME or "Accology")
        y -= 22 * mm

    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 16)
    cnv.drawString(left, y, "INVOICE")
    cnv.setFont("Helvetica-Bold", 12)
    cnv.drawRightString(right, y, invoice.number or "")
    y -= 6 * mm
    cnv.setFillColor(muted)
    cnv.setFont("Helvetica", 9)
    cnv.drawString(left, y, f"Issue date  {format_uk_date(invoice.issue_date)}")
    cnv.drawRightString(right, y, f"Due date  {format_uk_date(invoice.due_date)}")
    y -= 4 * mm
    if PRACTICE_EMAIL or PRACTICE_PHONE:
        bits = [b for b in (PRACTICE_EMAIL, PRACTICE_PHONE) if b]
        cnv.drawString(left, y, "  ·  ".join(bits))
        y -= 8 * mm
    else:
        y -= 4 * mm

    cnv.setStrokeColor(cyan)
    cnv.setLineWidth(1.4)
    cnv.line(left, y, right, y)
    y -= 8 * mm

    cnv.setFillColor(muted)
    cnv.setFont("Helvetica-Bold", 8)
    cnv.drawString(left, y, "BILL TO")
    y -= 5 * mm
    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 11)
    name = client.display_name() if client else "Client"
    cnv.drawString(left, y, name)
    y -= 4.5 * mm
    cnv.setFont("Helvetica", 9)
    if client:
        if client.address_line1:
            cnv.drawString(left, y, client.address_line1)
            y -= 4 * mm
        if client.address_line2:
            cnv.drawString(left, y, client.address_line2)
            y -= 4 * mm
        town_line = " ".join(
            p for p in ((client.town or "").strip(), (client.postcode or "").strip()) if p
        )
        if town_line:
            cnv.drawString(left, y, town_line)
            y -= 4 * mm
    y -= 6 * mm

    cols = [
        (left, "Service"),
        (left + 78 * mm, "Period end"),
        (left + 108 * mm, "Qty"),
        (left + 122 * mm, "VAT"),
        (right, "Amount"),
    ]
    cnv.setFillColor(muted)
    cnv.setFont("Helvetica-Bold", 8)
    cnv.drawString(cols[0][0], y, cols[0][1])
    cnv.drawString(cols[1][0], y, cols[1][1])
    cnv.drawRightString(cols[2][0] + 12 * mm, y, cols[2][1])
    cnv.drawRightString(cols[3][0] + 14 * mm, y, cols[3][1])
    cnv.drawRightString(cols[4][0], y, cols[4][1])
    y -= 2 * mm
    cnv.setStrokeColor(line_grey)
    cnv.setLineWidth(0.6)
    cnv.line(left, y, right, y)
    y -= 6 * mm

    cnv.setFillColor(navy)
    cnv.setFont("Helvetica", 9)
    for ln in invoice.lines or []:
        if y < 40 * mm:
            cnv.showPage()
            y = page_h - 20 * mm
            cnv.setFillColor(navy)
            cnv.setFont("Helvetica", 9)
        label = _line_label(ln, job)
        if len(label) > 48:
            label = label[:47] + "…"
        pe = format_uk_date(ln.period_end) if ln.period_end else (
            format_uk_date(job.period_end) if job and job.period_end else "—"
        )
        vat = float(ln.vat_rate or 0)
        vat_s = f"{vat * 100:.0f}%" if vat > 0.001 else "—"
        cnv.drawString(left, y, label)
        cnv.drawString(left + 78 * mm, y, pe)
        cnv.drawRightString(left + 120 * mm, y, f"{float(ln.qty or 0):.2f}")
        cnv.drawRightString(left + 136 * mm, y, vat_s)
        cnv.drawRightString(right, y, _money(ln.line_total))
        y -= 6 * mm

    y -= 4 * mm
    cnv.setStrokeColor(line_grey)
    cnv.line(left + 100 * mm, y, right, y)
    y -= 7 * mm
    cnv.setFont("Helvetica", 9)
    cnv.drawString(left + 100 * mm, y, "Subtotal")
    cnv.drawRightString(right, y, _money(invoice.subtotal))
    y -= 5 * mm
    cnv.drawString(left + 100 * mm, y, "VAT")
    cnv.drawRightString(right, y, _money(invoice.vat_total))
    y -= 6 * mm
    cnv.setFont("Helvetica-Bold", 11)
    cnv.drawString(left + 100 * mm, y, "Total")
    cnv.drawRightString(right, y, _money(invoice.total))
    y -= 6 * mm
    cnv.setFont("Helvetica", 9)
    cnv.drawString(left + 100 * mm, y, "Balance due")
    cnv.drawRightString(right, y, _money(invoice.balance))

    if invoice.notes:
        y -= 14 * mm
        cnv.setFillColor(muted)
        cnv.setFont("Helvetica-Bold", 8)
        cnv.drawString(left, y, "NOTES")
        y -= 5 * mm
        cnv.setFillColor(navy)
        cnv.setFont("Helvetica", 9)
        for part in str(invoice.notes).splitlines() or [""]:
            cnv.drawString(left, y, part[:110])
            y -= 4 * mm

    cnv.setFillColor(muted)
    cnv.setFont("Helvetica", 8)
    cnv.drawString(left, 16 * mm, "Thanks for coming, please call again")
    cnv.save()
    return buf.getvalue()
