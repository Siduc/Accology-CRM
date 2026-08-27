"""Pull one Xero client at year end and write a DRAFT pack + IRIS TB + Queries.

Uses YTD Debit / YTD Credit from the Xero trial balance (period Debit/Credit
are last-month only). Maps names onto the Accology Chart. Does not invent
figures, post journals, or file.

Run against local crm.db. Does not edit .env.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{(ROOT / 'crm.db').as_posix()}")

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.database import SessionLocal
from app.models.client import Client
from app.services.client_playbook import client_root_path, get_or_create_playbook
from app.services.iris_elements import load_chart_names, map_name, ye_header, write_iris_csv
from app.services.xero_books import pull_client_books, xero_get, _flatten_report, _write_csv
from app.services.xero_oauth import get_valid_access_token

NAVY = "052891"
CODE_RE = re.compile(r"\s*\((\d{1,6})\)\s*$")
SKIP_ACCOUNTS = re.compile(
    r"^(total\b|net profit\b|gross profit\b|current year earnings|"
    r"profit for the year|net assets\b|total assets|total liabilities|"
    r"total equity|total bank|total current|total fixed)",
    re.I,
)
MONEY_RE = re.compile(r"[£,\s]")

# Xero (or stripped) name -> Accology Chart name. Only when the source
# wording is unambiguous. Weak matches stay for map_name / Queries.
ALIASES = {
    "sales": "Turnover - Sales",
    "sales 200": "Turnover - Sales",
    "income": "Turnover - Sales",
    "turnover": "Turnover - Sales",
    "fixed water charge": "Turnover - Sales",
    "other revenue": "Turnover - Sales",
    "other income": "Other operating income",
    "fees": "Turnover - Fees",
    "billable hours": "Turnover - Fees",
    "hardware": "Cost of sales - Purchases",
    "hardware 5000": "Cost of sales - Purchases",
    "web services": "Cost of sales - Other direct costs",
    "web services 5004": "Cost of sales - Other direct costs",
    "dividends paid": "Profit and loss account - Equity dividends",
    "bad debt provision": "Administrative expenses - General - Bad debts",
    "audit and accountancy fees": "Administrative expenses - Legal & professional - Accountancy fees",
    "interest income": "Interest receivable",
    "interest received": "Interest receivable",
    "interest expense": "Interest payable - Other loans",
    "interest paid": "Interest payable - Other loans",
    "loan interest": "Interest payable - Other loans",
    "hp interest": "Interest payable - Finance leases and HP",
    "cost of goods sold": "Cost of sales - Purchases",
    "cost of sales": "Cost of sales - Purchases",
    "purchases": "Cost of sales - Purchases",
    "subcontractors": "Cost of sales - Subcontractor costs",
    "subcontractor costs": "Cost of sales - Subcontractor costs",
    "direct costs": "Cost of sales - Other direct costs",
    "direct expenses": "Cost of sales - Other direct costs",
    "travel international": "Administrative expenses - Employee costs - Travel and subsistence",
    "travel national": "Administrative expenses - Employee costs - Travel and subsistence",
    "sixty six south limited": "Cash - Cash at bank and in hand",
    "suspense": "Debtors - Other debtors",
    "external water": "Cost of sales - Other direct costs",
    "advertising": "Administrative expenses - Legal & professional - Advertising and PR",
    "advertising & marketing": "Administrative expenses - Legal & professional - Advertising and PR",
    "advertising and marketing": "Administrative expenses - Legal & professional - Advertising and PR",
    "marketing": "Administrative expenses - Legal & professional - Advertising and PR",
    "audit & accountancy fees": "Administrative expenses - Legal & professional - Accountancy fees",
    "audit and accountancy fees": "Administrative expenses - Legal & professional - Accountancy fees",
    "accountancy": "Administrative expenses - Legal & professional - Accountancy fees",
    "accountancy fees": "Administrative expenses - Legal & professional - Accountancy fees",
    "accountants fees": "Administrative expenses - Legal & professional - Accountancy fees",
    "bank fees": "Administrative expenses - General - Bank charges",
    "bank charges": "Administrative expenses - General - Bank charges",
    "bank fees and charges": "Administrative expenses - General - Bank charges",
    "depreciation": "Administrative expenses - General - Depreciation",
    "depreciation expense": "Administrative expenses - General - Depreciation",
    "general expenses": "Administrative expenses - General - Sundry expenses",
    "sundry": "Administrative expenses - General - Sundry expenses",
    "sundry expenses": "Administrative expenses - General - Sundry expenses",
    "office expenses": "Administrative expenses - General - Sundry expenses",
    "insurance": "Administrative expenses - General - Insurance",
    "it expense": "Administrative expenses - General - Software",
    "it software and consumables": "Administrative expenses - General - Software",
    "computer expenses": "Administrative expenses - General - Software",
    "software": "Administrative expenses - General - Software",
    "motor vehicle expenses": "Administrative expenses - Employee costs - Motor expenses",
    "motor expenses": "Administrative expenses - Employee costs - Motor expenses",
    "motor running": "Administrative expenses - Employee costs - Motor expenses",
    "travel": "Administrative expenses - Employee costs - Travel and subsistence",
    "travel and subsistence": "Administrative expenses - Employee costs - Travel and subsistence",
    "travelling": "Administrative expenses - Employee costs - Travel and subsistence",
    "professional fees": "Administrative expenses - Legal & professional - Other legal and professional",
    "professional fees payroll": "Administrative expenses - Legal & professional - Other legal and professional",
    "legal fees": "Administrative expenses - Legal & professional - Solicitors fees",
    "legal and professional": "Administrative expenses - Legal & professional - Other legal and professional",
    "consultancy": "Administrative expenses - Legal & professional - Consultancy fees",
    "consulting": "Administrative expenses - Legal & professional - Consultancy fees",
    "rent": "Administrative expenses - Premises costs - Rent",
    "rates": "Administrative expenses - Premises costs - Rates",
    "light and heat": "Administrative expenses - Premises costs - Light and heat",
    "electricity": "Administrative expenses - Premises costs - Light and heat",
    "gas": "Administrative expenses - Premises costs - Light and heat",
    "utilities": "Administrative expenses - Premises costs - Light and heat",
    "cleaning": "Administrative expenses - Premises costs - Cleaning",
    "repairs": "Administrative expenses - General - Repairs and maintenance",
    "repairs and maintenance": "Administrative expenses - General - Repairs and maintenance",
    "repairs and renewals": "Administrative expenses - General - Repairs and maintenance",
    "telephone": "Administrative expenses - General - Telephone and fax",
    "telephone & internet": "Administrative expenses - General - Telephone and fax",
    "telephone and internet": "Administrative expenses - General - Telephone and fax",
    "internet": "Administrative expenses - General - Internet",
    "postage": "Administrative expenses - General - Postage",
    "postage freight and courier": "Administrative expenses - General - Courier services",
    "carriage out": "Cost of sales - Carriage",
    "carriage in": "Cost of sales - Carriage",
    "entertainment 0": "Administrative expenses - Employee costs - Entertaining",
    "stripe fees": "Administrative expenses - General - Bank charges",
    "travel national subsistence": "Administrative expenses - Employee costs - Travel and subsistence",
    "full display ltd": "Cash - Cash at bank and in hand",
    "owner a drawings": "Creditors less than 1 year - Directors' loans",
    "owner a funds introduced": "Creditors less than 1 year - Directors' loans",
    "retained earnings equity dividends": "Profit and loss account - Equity dividends",
    "printing and stationery": "Administrative expenses - General - Stationery and printing",
    "stationery": "Administrative expenses - General - Stationery and printing",
    "subscriptions": "Administrative expenses - General - Subscriptions",
    "salaries": "Administrative expenses - Employee costs - Wages and salaries",
    "wages": "Administrative expenses - Employee costs - Wages and salaries",
    "wages and salaries": "Administrative expenses - Employee costs - Wages and salaries",
    "directors remuneration": "Administrative expenses - Employee costs - Directors' salaries",
    "directors salaries": "Administrative expenses - Employee costs - Directors' salaries",
    "directors salary": "Administrative expenses - Employee costs - Directors' salaries",
    "employers ni": "Administrative expenses - Employee costs - Employer's NI",
    "employer ni": "Administrative expenses - Employee costs - Employer's NI",
    "pensions": "Administrative expenses - Employee costs - Pensions",
    "pension costs": "Administrative expenses - Employee costs - Pensions",
    "staff training": "Administrative expenses - Employee costs - Staff training and welfare",
    "entertainment": "Administrative expenses - Employee costs - Entertaining",
    "entertaining": "Administrative expenses - Employee costs - Entertaining",
    "bad debts": "Administrative expenses - General - Bad debts",
    "corporation tax": "Taxation - Corporation tax",
    "accounts receivable": "Debtors - Trade debtors",
    "trade debtors": "Debtors - Trade debtors",
    "trade receivables": "Debtors - Trade debtors",
    "amounts recoverable on contracts": "Debtors - Accrued income and prepayments",
    "prepayments": "Debtors - Accrued income and prepayments",
    "accrued income": "Debtors - Accrued income and prepayments",
    "other debtors": "Debtors - Other debtors",
    "accounts payable": "Creditors less than 1 year - Trade creditors",
    "trade creditors": "Creditors less than 1 year - Trade creditors",
    "trade payables": "Creditors less than 1 year - Trade creditors",
    "accruals": "Creditors less than 1 year - Accruals",
    "vat": "Creditors less than 1 year - Other taxes and social security",
    "vat control": "Creditors less than 1 year - Other taxes and social security",
    "paye": "Creditors less than 1 year - Other taxes and social security",
    "paye payable": "Creditors less than 1 year - Other taxes and social security",
    "paye & ni": "Creditors less than 1 year - Other taxes and social security",
    "pensions payable": "Creditors less than 1 year - Other taxes and social security",
    "directors loan": "Creditors less than 1 year - Directors' loans",
    "directors loan account": "Creditors less than 1 year - Directors' loans",
    "director's loan account": "Creditors less than 1 year - Directors' loans",
    "directors current account": "Creditors less than 1 year - Directors' loans",
    "retained earnings": "Profit and loss account - Brought forward",
    "profit and loss account": "Profit and loss account - Brought forward",
    "share capital": "Share capital - Brought forward",
    "ordinary shares": "Share capital - Brought forward",
    "ordinary share capital": "Share capital - Brought forward",
    "capital x xxx ordinary shares": "Share capital - Brought forward",
    "acc util uk": "Cash - Cash at bank and in hand",
    "amberis legal": "Creditors less than 1 year - Other creditors",
    "north west loans": "Creditors less than 1 year - Bank loans",
    "bob brearley loan": "Debtors - Other debtors",
    "martin doyle loan": "Debtors - Other debtors",
    "phil davies loan": "Debtors - Other debtors",
    "parker colby loan": "Creditors less than 1 year - Other creditors",
    "rounding": "Debtors - Other debtors",
    "stock": "Stocks - Finished goods",
    "inventory": "Stocks - Finished goods",
    "stock on hand": "Stocks - Finished goods",
    "cash": "Cash - Cash at bank and in hand",
    "cash on hand": "Cash - Cash at bank and in hand",
    "petty cash": "Cash - Cash at bank and in hand",
    "bank": "Cash - Cash at bank and in hand",
    "current account": "Cash - Cash at bank and in hand",
    "business account": "Cash - Cash at bank and in hand",
    "bank account": "Cash - Cash at bank and in hand",
    "overdraft": "Creditors less than 1 year - Overdrafts",
    "credit card": "Creditors less than 1 year - Other creditors",
    "credit card control account": "Creditors less than 1 year - Other creditors",
    "dividends": "Profit and loss account - Equity dividends",
    "dividend": "Profit and loss account - Equity dividends",
    "plant and machinery": "Plant & machinery - Cost - b/fwd",
    "plant & machinery": "Plant & machinery - Cost - b/fwd",
    "less accumulated depreciation on plant and machinery": "Plant & machinery - Depn - b/fwd",
    "accumulated depreciation on plant and machinery": "Plant & machinery - Depn - b/fwd",
    "computer equipment": "Computer equipment - Cost - b/fwd",
    "less accumulated depreciation on computer equipment": "Computer equipment - Depn - b/fwd",
    "accumulated depreciation on computer equipment": "Computer equipment - Depn - b/fwd",
    "motor vehicles": "Motor vehicles - Cost - b/fwd",
    "less accumulated depreciation on motor vehicles": "Motor vehicles - Depn - b/fwd",
    "fixtures and fittings": "Fixtures & fittings - Cost - b/fwd",
    "less accumulated depreciation on fixtures and fittings": "Fixtures & fittings - Depn - b/fwd",
    "office equipment": "Fixtures & fittings - Cost - b/fwd",
    "land and buildings": "Land & buildings - Cost - b/fwd",
    "deferred tax": "Deferred tax - Brought forward",
    "corporation tax payable": "Creditors less than 1 year - Corporation tax",
    "hire purchase": "Creditors less than 1 year - Finance lease and HP contracts",
    "hp creditor": "Creditors less than 1 year - Finance lease and HP contracts",
    "finance lease": "Creditors less than 1 year - Finance lease and HP contracts",
    "bank loan": "Creditors less than 1 year - Bank loans",
    "profit on flat rate vat": "Other operating income",
    "direct wages": "Cost of sales - Direct labour",
    "gifts": "Administrative expenses - General - Donations",
    "legal expenses": "Administrative expenses - Legal & professional - Solicitors fees",
    "storage": "Administrative expenses - Premises costs - Rent",
    "less accumulated depreciation on office equipment": "Fixtures & fittings - Depn - b/fwd",
    "office equipment": "Fixtures & fittings - Cost - b/fwd",
    "starling business account": "Cash - Cash at bank and in hand",
    "provision for corporation tax": "Creditors less than 1 year - Corporation tax",
    "corporation tax": "Creditors less than 1 year - Corporation tax",
    "barclaycard": "Cash - Cash at bank and in hand",
    "company vehicles": "Motor vehicles - Cost - b/fwd",
    "debtors control account": "Debtors - Trade debtors",
    "douglas and lord debtor": "Debtors - Trade debtors",
    "furniture and fixture depreciation": "Fixtures & fittings - Depn - b/fwd",
    "furniture and fixtures": "Fixtures & fittings - Cost - b/fwd",
    "interco fortitude": "Debtors - Due from group undertakings",
    "jl jd joint loan account p": "Creditors less than 1 year - Directors' loans",
    "office equipment depreciation": "Fixtures & fittings - Depn - b/fwd",
    "paypal": "Cash - Cash at bank and in hand",
    "paypal 1236": "Cash - Cash at bank and in hand",
    "petty cash": "Cash - Cash at bank and in hand",
    "recharges pending": "Debtors - Other debtors",
    "structured outlooks current 10569984": "Cash - Cash at bank and in hand",
    "structured outlooks holding 60888680": "Cash - Cash at bank and in hand",
    "structured outlooks tracker 30803413": "Cash - Cash at bank and in hand",
    "companies house": "Creditors less than 1 year - Other creditors",
    "creditors control account": "Creditors less than 1 year - Trade creditors",
    "directors loan account jd": "Creditors less than 1 year - Directors' loans",
    "directors loan account jl": "Creditors less than 1 year - Directors' loans",
    "hsf health plans": "Creditors less than 1 year - Other taxes and social security",
    "paye and national insurance": "Creditors less than 1 year - Other taxes and social security",
    "p a y e and national insurance": "Creditors less than 1 year - Other taxes and social security",
    "simply health": "Creditors less than 1 year - Other taxes and social security",
    "student loan": "Creditors less than 1 year - Other creditors",
    "vat liability": "Creditors less than 1 year - Other taxes and social security",
    "net wages": "Creditors less than 1 year - Other taxes and social security",
    "motor vehicle depreciation": "Motor vehicles - Depn - b/fwd",
    "motor vehicles depreciation": "Motor vehicles - Depn - b/fwd",
    "electric gas and water": "Administrative expenses - Premises costs - Light and heat",
    "employers national insurance": "Administrative expenses - Employee costs - Employer's NI",
    "entertainment 100 business": "Administrative expenses - Employee costs - Entertaining",
    "garden and rubbish removal": "Administrative expenses - Premises costs - Cleaning",
    "housekeeping": "Cost of sales - Other direct costs",
    "rates and service charges": "Administrative expenses - Premises costs - Rates",
    "rent to landlords": "Administrative expenses - Premises costs - Rent",
    "revolut merchant fees": "Administrative expenses - General - Bank charges",
    "website fees commissions": "Administrative expenses - Legal & professional - Advertising and PR",
    "nic payable": "Creditors less than 1 year - Other taxes and social security",
    "student loan deductions payable": "Creditors less than 1 year - Other taxes and social security",
    "wages payable payroll": "Creditors less than 1 year - Other taxes and social security",
    "loans": "Creditors less than 1 year - Other creditors",
    "loan": "Creditors less than 1 year - Other creditors",
}


def money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = MONEY_RE.sub("", str(value).strip())
    if not s or s in {"-", "–", "—"}:
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def money0(value: Any) -> float:
    v = money(value)
    return 0.0 if v is None else v


def norm_key(name: str) -> str:
    s = CODE_RE.sub("", name or "")
    s = re.sub(r"[^a-z0-9&]+", " ", s.lower()).strip()
    s = s.replace("&", "and")
    s = re.sub(r"\s+", " ", s)
    return s


def strip_code(name: str) -> str:
    return CODE_RE.sub("", name or "").strip()


def map_xero_name(
    source: str, names: Sequence[str], company: str = ""
) -> Tuple[str, float, str]:
    raw = (source or "").strip()
    if not raw:
        return "", 0.0, "empty"
    stripped = strip_code(raw)
    key = norm_key(stripped)
    if key in ALIASES:
        dest = ALIASES[key]
        return dest, 1.0, "alias"
    if company and norm_key(company) == key:
        return "Cash - Cash at bank and in hand", 0.9, "alias"
    if key.startswith("revolut") or key.startswith("starling") or key.startswith("monzo"):
        if "fee" in key or "merchant" in key:
            return "Administrative expenses - General - Bank charges", 0.95, "alias"
        return "Cash - Cash at bank and in hand", 0.9, "alias"
    # prefix-stripped bank-like names
    if key.startswith("acc ") or "bank" in key or key.endswith(" ltd") and "loan" not in key:
        pass
    if "ordinary share" in key or key.startswith("capital ") and "share" in key:
        return "Share capital - Brought forward", 0.95, "alias"
    dest, score = map_name(stripped, names)
    if dest in names:
        return dest, score, "fuzzy"
    dest2, score2 = map_name(raw, names)
    if dest2 in names and score2 > score:
        return dest2, score2, "fuzzy"
    return dest if dest in names else stripped, max(score, score2), "unmapped"


def parse_xero_tb(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    headers = [h.strip() for h in (rows[0].keys() if rows else [])]
    ytd_dr = next((h for h in headers if h.lower() in {"ytd debit", "ytd debits"}), None)
    ytd_cr = next((h for h in headers if h.lower() in {"ytd credit", "ytd credits"}), None)
    acc_h = next((h for h in headers if h.lower() in {"account", "col", "name"}), None)
    sec_h = next((h for h in headers if h.lower() == "section"), None)
    dr_h = next((h for h in headers if h.lower() == "debit"), None)
    cr_h = next((h for h in headers if h.lower() == "credit"), None)
    out: List[Dict[str, Any]] = []
    for rec in rows:
        acc = str((rec.get(acc_h) if acc_h else "") or "").strip()
        if not acc or SKIP_ACCOUNTS.search(acc):
            continue
        if ytd_dr or ytd_cr:
            debit = money0(rec.get(ytd_dr) if ytd_dr else None)
            credit = money0(rec.get(ytd_cr) if ytd_cr else None)
        else:
            debit = money0(rec.get(dr_h) if dr_h else None)
            credit = money0(rec.get(cr_h) if cr_h else None)
        amount = round(debit - credit, 2)
        if abs(amount) < 0.005:
            continue
        out.append(
            {
                "section": str((rec.get(sec_h) if sec_h else "") or "").strip(),
                "source": acc,
                "debit": debit,
                "credit": credit,
                "amount": amount,
            }
        )
    return out


def classify(iris: str) -> str:
    n = (iris or "").lower()
    if n.startswith("turnover"):
        return "turnover"
    if n.startswith("cost of sales") or n == "distribution costs":
        return "cos"
    if n.startswith("administrative"):
        return "admin"
    if n.startswith("other operating"):
        return "other_income"
    if n.startswith("investment income") or n.startswith("gains and losses"):
        return "gains"
    if n == "interest receivable":
        return "int_rec"
    if n.startswith("interest payable"):
        return "int_pay"
    if n.startswith("taxation"):
        return "tax"
    if "cost -" in n or n.endswith("cost - b/fwd") or "cost - additions" in n:
        return "fa_cost"
    if "depn" in n or "amortisation" in n:
        return "fa_dep"
    if n.startswith("investment") or n.startswith("intangible"):
        return "fa_other"
    if n.startswith("stocks"):
        return "stock"
    if n.startswith("debtors"):
        return "debtors"
    if n.startswith("cash") or n.startswith("current asset investments"):
        return "cash"
    if n.startswith("creditors less than 1 year"):
        return "cl"
    if n.startswith("creditors greater than 1 year"):
        return "ncl"
    if n.startswith("deferred tax") or n.startswith("provisions"):
        return "prov"
    if n.startswith("share") or n.startswith("revaluation") or n.startswith("capital"):
        return "equity"
    if n.startswith("profit and loss"):
        return "pl_res"
    return "other"


def style_header(ws, row: int, cols: int) -> None:
    fill = PatternFill("solid", fgColor=NAVY)
    font = Font(color="FFFFFF", bold=True, name="Calibri")
    for col in range(1, cols + 1):
        cell = ws.cell(row, col)
        cell.fill = fill
        cell.font = font


def write_money(ws, r: int, c: int, value) -> None:
    cell = ws.cell(r, c, round(float(value or 0), 2))
    cell.number_format = '#,##0.00;(#,##0.00);"—"'
    cell.alignment = Alignment(horizontal="right")


def prior_year_start(as_at: date) -> date:
    try:
        prev = date(as_at.year - 1, as_at.month, as_at.day)
    except ValueError:
        last = monthrange(as_at.year - 1, as_at.month)[1]
        prev = date(as_at.year - 1, as_at.month, last)
    return prev + timedelta(days=1)


def refetch_year_pl(
    db, tenant_id: str, as_at: date, source_dir: Path
) -> Tuple[str, int]:
    token, err, _ = get_valid_access_token(db)
    if not token:
        return err or "no token", 0
    start = prior_year_start(as_at)
    ok, raw, rerr = xero_get(
        token,
        tenant_id,
        "/Reports/ProfitAndLoss",
        params={"fromDate": start.isoformat(), "toDate": as_at.isoformat()},
    )
    if not ok or not isinstance(raw, dict):
        return rerr or "P&L refetch failed", 0
    headers, rows = _flatten_report(raw)
    name = f"{as_at.isoformat()} Profit and Loss year.csv"
    _write_csv(source_dir / name, headers or ["Account"], rows)
    return name, len(rows)


def build_queries(
    *,
    company: str,
    as_at: date,
    playbook_ye: Optional[date],
    lines: List[Dict[str, Any]],
    buckets: Dict[str, float],
    unknown: List[str],
    counts: Dict[str, Any],
    notes: List[str],
) -> List[str]:
    q: List[str] = []
    q.extend(notes)
    if playbook_ye and playbook_ye != as_at:
        q.append(
            f"Playbook year end is {playbook_ye.strftime('%d/%m/%Y')}; this pack "
            f"is as at {as_at.strftime('%d/%m/%Y')} (open Accounts job / prior papers). Confirm the ARD."
        )
    if unknown:
        q.append(
            "Unmapped Xero names (left on the IRIS TB under the Xero wording — "
            f"recode before import): {', '.join(unknown)}."
        )
    # Odd signs
    for line in lines:
        name = line["source"]
        amt = line["amount"]
        sec = (line.get("section") or "").lower()
        if sec.startswith("exp") and amt < -0.005:
            q.append(
                f"{name} is a YTD credit of £{abs(amt):,.2f} on expenses — refund, "
                "mispost, or income in the wrong account?"
            )
        if sec.startswith("rev") and amt > 0.005:
            q.append(
                f"{name} is a YTD debit of £{amt:,.2f} on revenue — confirm."
            )
        low = name.lower()
        if "round" in low and abs(amt) >= 1:
            q.append(
                f"{name} is £{amt:,.2f} — do not file with a rounding / suspense "
                "balance this size. Analyse and reallocate."
            )
        if set(strip_code(name)) <= set("*") or strip_code(name).startswith("****"):
            q.append(
                f"Unidentified account {name!r} £{amt:,.2f} — rename in Xero before finals."
            )
    # Missing CT
    tax = sum(v for k, v in buckets.items() if k.startswith("Taxation"))
    pbt_like = sum(
        v
        for k, v in buckets.items()
        if classify(k) in {"turnover", "cos", "admin", "other_income", "gains", "int_rec", "int_pay"}
    )
    # turnover is credit (neg). PBT = -sum of those (since credits neg)
    has_ct_creditor = any("corporation tax" in k.lower() for k in buckets)
    if abs(tax) < 0.005 and has_ct_creditor:
        q.append(
            "A corporation tax creditor/provision is on the Xero TB but there is no "
            "P&L tax charge. CT not recomputed (needs capital allowances / losses)."
        )
    elif abs(tax) < 0.005:
        q.append(
            "No corporation tax charge or creditor on the Xero TB. CT not computed "
            "(needs capital allowances / losses). Left blank — do not invent."
        )
    if not any(classify(k) == "equity" for k in buckets):
        q.append(
            "No share capital (or other equity) line on the Xero TB. Confirm allotment "
            "and whether Xero is missing the capital account."
        )
    for name, amt in buckets.items():
        kind = classify(name)
        if kind == "cl" and amt > 0.005:
            q.append(
                f"{name} is a debit of £{amt:,.2f} on a creditor heading "
                "(overdrawn DLA / mispost). Confirm whether this is a debtor."
            )
        if kind == "debtors" and amt < -0.005:
            q.append(
                f"{name} is a credit of £{abs(amt):,.2f} on a debtor heading "
                "(suspense / credit balance). Confirm whether this is a creditor."
            )
    # Unchanged contract / prepayment often needs confirm
    q.append(
        "Fixed-asset split (b/fwd vs additions vs charge) is the Xero closing "
        "cost / accum depn as presented. Confirm additions, disposals and rates "
        "from the asset register before IRIS FA notes."
    )
    q.append(
        "No year-end journals have been posted back to Xero. Accruals, prepayments, "
        "stock, DLA agreement and CT stay as queries until you confirm."
    )
    if not counts.get("bank_transactions"):
        q.append("No bank transactions came down for the year — bank rec not possible from this pull.")
    elif int(counts.get("bank_transactions") or 0) >= 2000:
        q.append(
            "Bank transaction pull hit the 2,000-row page cap. The bank CSV is incomplete "
            "for the year — do not treat it as a full rec."
        )
    pl_rows = [ln for ln in lines if (ln.get("section") or "").lower() in {"revenue", "expenses", "income"}]
    if not pl_rows:
        q.append(
            "Xero trial balance has no revenue or expense lines for this year end "
            "(P&L is nil). Confirm whether the company was dormant / books not written "
            "up, or whether the Xero organisation is the wrong file."
        )
    asset_liab = [
        ln
        for ln in lines
        if (ln.get("section") or "").lower() in {"assets", "liabilities", "bank"}
    ]
    if not asset_liab:
        q.append(
            "Xero trial balance as at year end has no asset or liability lines "
            "(balance sheet is nil except equity / current-year earnings). Confirm "
            "whether the company was stripped, wound down, or the books were closed "
            "out — last year's comparatives are on the pulled Balance Sheet CSV."
        )
    # Related-party / loan names
    loan_like = [
        ln
        for ln in lines
        if re.search(r"\b(loan|director|dla)\b", ln["source"], re.I)
    ]
    if loan_like:
        bits = ", ".join(f"{ln['source']} £{ln['amount']:,.2f}" for ln in loan_like)
        q.append(
            f"Loan / director balances on the TB (confirm <1yr / >1yr, related party, "
            f"and whether any are debit DLA / overdrawn): {bits}."
        )
    if pbt_like:
        pass
    return q


def write_pack(
    dest: Path,
    *,
    company: str,
    as_at: date,
    tenant_name: str,
    source_files: List[str],
    lines: List[Dict[str, Any]],
    buckets: Dict[str, float],
    queries: List[str],
    cover_stats: List[Tuple[str, Any]],
    pl_lines: List[Tuple[str, float]],
    bs_sections: List[Tuple[str, List[Tuple[str, float]]]],
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ye_label = as_at.strftime("%d %B %Y")
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = company
    cover["A1"].font = Font(name="Calibri", bold=True, size=18, color=NAVY)
    cover["A2"] = f"Draft accounts working pack — year ended {ye_label}"
    cover["A3"] = (
        f"Source: Xero organisation {tenant_name}. Trial balance YTD as at "
        f"{as_at.isoformat()}. Pulled files: {', '.join(source_files)}."
    )
    cover["A4"] = (
        "DRAFT only. NOT an IRIS statutory set. Do not file. Credits are negative "
        "on the IRIS Elements TB. Figures are Xero YTD — nothing invented."
    )
    cover["A4"].font = Font(name="Calibri", italic=True, color="833C0C")
    r = 6
    cover.cell(r, 1, "Draft figures (from Xero TB YTD)")
    cover.cell(r, 1).font = Font(bold=True, color=NAVY)
    r += 1
    for label, val in cover_stats:
        cover.cell(r, 1, label)
        if isinstance(val, (int, float)):
            write_money(cover, r, 2, val)
        else:
            cover.cell(r, 2, val)
        r += 1
    r += 1
    cover.cell(r, 1, "Open queries")
    cover.cell(r, 1).font = Font(bold=True, color=NAVY)
    r += 1
    for i, q in enumerate(queries, 1):
        cover.cell(r, 1, f"{i}. {q}")
        cover.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
        cover.row_dimensions[r].height = 36
        r += 1
    cover.column_dimensions["A"].width = 110
    cover.column_dimensions["B"].width = 18

    qws = wb.create_sheet("Queries")
    qws["A1"] = "No"
    qws["B1"] = "Query"
    qws["C1"] = "Blocks IRIS final?"
    style_header(qws, 1, 3)
    for i, q in enumerate(queries, 1):
        qws.cell(i + 1, 1, i)
        qws.cell(i + 1, 2, q)
        qws.cell(i + 1, 2).alignment = Alignment(wrap_text=True, vertical="top")
        qws.cell(i + 1, 3, "Yes — confirm before filing")
        qws.row_dimensions[i + 1].height = 36
    qws.column_dimensions["A"].width = 6
    qws.column_dimensions["B"].width = 110
    qws.column_dimensions["C"].width = 28

    tb = wb.create_sheet("Xero TB YTD")
    tb["A1"] = "Section"
    tb["B1"] = "Xero account"
    tb["C1"] = "YTD Debit"
    tb["D1"] = "YTD Credit"
    tb["E1"] = "Signed (credits −)"
    tb["F1"] = "Accology / IRIS name"
    tb["G1"] = "Map"
    style_header(tb, 1, 7)
    r = 2
    tot_dr = tot_cr = 0.0
    for line in lines:
        tb.cell(r, 1, line.get("section") or "")
        tb.cell(r, 2, line["source"])
        write_money(tb, r, 3, line["debit"])
        write_money(tb, r, 4, line["credit"])
        write_money(tb, r, 5, line["amount"])
        tb.cell(r, 6, line["iris"])
        tb.cell(r, 7, line["map_how"])
        tot_dr += line["debit"]
        tot_cr += line["credit"]
        r += 1
    tb.cell(r, 2, "Total")
    tb.cell(r, 2).font = Font(bold=True)
    write_money(tb, r, 3, tot_dr)
    write_money(tb, r, 4, tot_cr)
    write_money(tb, r, 5, tot_dr - tot_cr)
    for col, w in enumerate([18, 56, 16, 16, 18, 62, 12], 1):
        tb.column_dimensions[get_column_letter(col)].width = w

    pl = wb.create_sheet("Draft P&L")
    pl["A1"] = f"Profit and loss — year ended {ye_label}"
    pl["A1"].font = Font(bold=True, size=14, color=NAVY)
    pl["A2"] = "Reconstructed from the Xero TB YTD after Accology Chart mapping. No extra journals."
    pl["A3"] = "Line"
    pl["B3"] = "£"
    style_header(pl, 3, 2)
    bold = {
        "Turnover",
        "Cost of sales",
        "Gross profit",
        "Administrative expenses",
        "Operating profit",
        "Profit before tax",
        "Profit after tax",
        "Profit for the year",
    }
    for i, (label, val) in enumerate(pl_lines, start=4):
        pl.cell(i, 1, label)
        write_money(pl, i, 2, val)
        if label in bold:
            pl.cell(i, 1).font = Font(bold=True)
    pl.column_dimensions["A"].width = 64
    pl.column_dimensions["B"].width = 16

    bs = wb.create_sheet("Draft balance sheet")
    bs["A1"] = f"Balance sheet — as at {ye_label}"
    bs["A1"].font = Font(bold=True, size=14, color=NAVY)
    bs["A2"] = "From Xero TB YTD. Loan / DLA split <1yr vs >1yr not confirmed — see Queries."
    r = 4
    for title, rows in bs_sections:
        bs.cell(r, 1, title)
        bs.cell(r, 2, "£")
        style_header(bs, r, 2)
        r += 1
        for label, val in rows:
            bs.cell(r, 1, label)
            write_money(bs, r, 2, val)
            low = label.lower()
            if low.startswith("total") or low.startswith("net") or "funds" in low:
                bs.cell(r, 1).font = Font(bold=True)
            r += 1
        r += 1
    bs.column_dimensions["A"].width = 64
    bs.column_dimensions["B"].width = 16

    iris = wb.create_sheet("IRIS Elements TB")
    iris["A1"] = "Account"
    iris["B1"] = "Description"
    iris["C1"] = ye_header(as_at)
    style_header(iris, 1, 3)
    r = 2
    net = 0.0
    for account in sorted(buckets):
        amt = round(buckets[account], 2)
        if abs(amt) < 0.005:
            continue
        iris.cell(r, 1, account)
        iris.cell(r, 2, account)
        write_money(iris, r, 3, amt)
        net += amt
        r += 1
    iris.cell(r, 1, "Net (must be 0.00)")
    iris.cell(r, 1).font = Font(bold=True)
    write_money(iris, r, 3, net)
    iris.column_dimensions["A"].width = 64
    iris.column_dimensions["B"].width = 64
    iris.column_dimensions["C"].width = 20
    wb.save(dest)


def write_queries_md(
    path: Path, company: str, as_at: date, queries: List[str], pulled: List[str], net: float
) -> None:
    lines = [
        f"# {company} — draft queries",
        "",
        f"Year end {as_at.strftime('%d/%m/%Y')}. DRAFT only. Confirm before IRIS / CH / HMRC.",
        f"IRIS TB net (must be 0.00): {net:,.2f}.",
        "",
        "## Pulled from Xero",
        "",
    ]
    for f in pulled:
        lines.append(f"- {f}")
    lines += ["", "## Queries", ""]
    for i, q in enumerate(queries, 1):
        lines.append(f"{i}. {q}")
    lines += [
        "",
        "## Not done",
        "",
        "- No journals posted back to Xero.",
        "- No IRIS import / statutory accounts / CT computation.",
        "- No Companies House or HMRC filing.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def statements_from_buckets(buckets: Dict[str, float]) -> Tuple[
    List[Tuple[str, float]],
    List[Tuple[str, List[Tuple[str, float]]]],
    List[Tuple[str, Any]],
    float,
    float,
]:
    groups: Dict[str, float] = defaultdict(float)
    detail: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for name, amt in sorted(buckets.items()):
        if abs(amt) < 0.005:
            continue
        kind = classify(name)
        groups[kind] += amt
        detail[kind].append((name, amt))

    # P&L presentation: credits (income) shown as positive
    turnover = -groups["turnover"]
    cos = groups["cos"]
    gross = round(turnover - cos, 2)
    admin = groups["admin"]
    other_inc = -groups["other_income"]
    gains = -groups["gains"]
    int_rec = -groups["int_rec"]
    int_pay = groups["int_pay"]
    tax = groups["tax"]
    op = round(gross - admin + other_inc + gains, 2)
    pbt = round(op + int_rec - int_pay, 2)
    pat = round(pbt - tax, 2)

    pl: List[Tuple[str, float]] = [("Turnover", turnover)]
    for n, a in detail["turnover"]:
        pl.append((f"  {n}", -a))
    pl.append(("Cost of sales", cos))
    for n, a in detail["cos"]:
        pl.append((f"  {n}", a))
    pl.append(("Gross profit", gross))
    pl.append(("Administrative expenses", admin))
    for n, a in detail["admin"]:
        pl.append((f"  {n}", a))
    if abs(other_inc) >= 0.005:
        pl.append(("Other operating income", other_inc))
    if abs(gains) >= 0.005:
        pl.append(("Gains / (losses)", gains))
    pl.append(("Operating profit", op))
    if abs(int_rec) >= 0.005:
        pl.append(("Interest receivable", int_rec))
    if abs(int_pay) >= 0.005:
        pl.append(("Interest payable", int_pay))
    pl.append(("Profit before tax", pbt))
    if abs(tax) >= 0.005:
        pl.append(("Taxation", tax))
    pl.append(("Profit for the year", pat))

    fa = round(groups["fa_cost"] + groups["fa_dep"] + groups["fa_other"], 2)
    ca = round(groups["stock"] + groups["debtors"] + groups["cash"], 2)
    # creditors are credits (negative). Display as positive liabilities.
    cl = -groups["cl"]
    ncl = -groups["ncl"]
    prov = -groups["prov"]
    net_ca = round(ca - cl, 2)
    other_bs = groups["other"]
    net_assets = round(fa + net_ca - ncl - prov + other_bs, 2)
    equity = -groups["equity"]
    pl_res = -groups["pl_res"]
    # current year sits in P&L lines still on TB — equity check = capital + RE + PAT
    # But PAT is already in the P&L TB lines, not in pl_res. So:
    #   funds = equity (share cap) + pl_res (RE b/fwd) + PAT
    # Wait: PAT is composed of P&L TB lines which are still in buckets.
    # BS assets - liabs should equal - (equity + pl_res + p&l lines)
    funds = round(equity + pl_res + pat, 2)

    def rows_signed(kind: str, flip: bool) -> List[Tuple[str, float]]:
        out = []
        for n, a in detail[kind]:
            out.append((n, -a if flip else a))
        return out

    bs: List[Tuple[str, List[Tuple[str, float]]]] = [
        (
            "Fixed assets",
            rows_signed("fa_cost", False)
            + rows_signed("fa_dep", False)
            + rows_signed("fa_other", False)
            + [("Total fixed assets", fa)],
        ),
        (
            "Current assets",
            rows_signed("stock", False)
            + rows_signed("debtors", False)
            + rows_signed("cash", False)
            + [("Total current assets", ca)],
        ),
        (
            "Creditors: amounts falling due within one year",
            rows_signed("cl", True) + [("Total creditors < 1 year", cl)],
        ),
        ("Net current assets / (liabilities)", [("Net current assets", net_ca)]),
        (
            "Creditors: amounts falling due after one year",
            rows_signed("ncl", True) + [("Total creditors > 1 year", ncl)],
        ),
        (
            "Provisions",
            rows_signed("prov", True) + [("Total provisions", prov)],
        ),
        ("Net assets", [("Net assets", net_assets)]),
        (
            "Capital and reserves",
            rows_signed("equity", True)
            + rows_signed("pl_res", True)
            + [("Profit for the year", pat)]
            + [("Shareholders' funds", funds)],
        ),
    ]
    if abs(other_bs) >= 0.005:
        bs.insert(
            -1,
            (
                "Unclassified (see Queries)",
                rows_signed("other", False) + [("Total unclassified", other_bs)],
            ),
        )

    cover = [
        ("Turnover", turnover),
        ("Gross profit", gross),
        ("Profit before tax", pbt),
        ("Profit for the year", pat),
        ("Net assets", net_assets),
        ("Shareholders' funds (should equal net assets)", funds),
        ("Funds vs net assets difference", round(funds - net_assets, 2)),
    ]
    return pl, bs, cover, pbt, net_assets


def process_client(
    db,
    client: Client,
    as_at: date,
    playbook_ye: Optional[date],
    extra_notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    extra_notes = list(extra_notes or [])
    pull = pull_client_books(db, client, as_at=as_at)
    result: Dict[str, Any] = {
        "client_id": client.id,
        "client": client.company_name,
        "as_at": as_at.isoformat(),
        "ok": False,
        "error": "",
        "pulled": pull.get("files") or [],
        "counts": pull.get("counts") or {},
        "tenant": pull.get("tenant_name") or "",
        "net": None,
        "unknown": [],
        "queries": [],
        "pack": "",
        "iris": "",
        "queries_md": "",
        "turnover": None,
        "pbt": None,
        "net_assets": None,
    }
    if not pull.get("ok"):
        result["error"] = pull.get("error") or "Xero pull failed"
        return result

    source_dir = Path(pull["source_dir"])
    tb_path = source_dir / f"{as_at.isoformat()} Trial Balance.csv"
    if not tb_path.is_file():
        result["error"] = f"No trial balance written: {tb_path}"
        return result

    # Better year P&L (the default pull is Xero's current period).
    try:
        pl_name, pl_n = refetch_year_pl(db, pull["tenant_id"], as_at, source_dir)
        if pl_n:
            result["pulled"].append(pl_name)
            result["counts"]["ProfitAndLoss_year"] = pl_n
        else:
            extra_notes.append(f"Year P&L refetch: {pl_name}")
    except Exception as exc:  # noqa: BLE001
        extra_notes.append(f"Year P&L refetch failed: {exc}")

    names = load_chart_names()
    if not names:
        result["error"] = "Accology Chart not found"
        return result

    raw_lines = parse_xero_tb(tb_path)
    if not raw_lines:
        result["error"] = f"Could not parse YTD columns from {tb_path.name}"
        return result

    buckets: Dict[str, float] = defaultdict(float)
    lines: List[Dict[str, Any]] = []
    unknown: List[str] = []
    for ln in raw_lines:
        dest, score, how = map_xero_name(
            ln["source"], names, client.company_name or ""
        )
        if dest not in names:
            how = "unmapped"
            unknown.append(ln["source"])
        elif score < 0.86 and how == "fuzzy":
            extra_notes.append(
                f"Soft map {ln['source']!r} → {dest!r} (score {score:.2f}) — confirm wording."
            )
        buckets[dest] = round(buckets[dest] + ln["amount"], 2)
        lines.append({**ln, "iris": dest, "score": score, "map_how": how})

    pl_lines, bs_sections, cover_stats, pbt, net_assets = statements_from_buckets(dict(buckets))
    turnover = next((v for k, v in cover_stats if k == "Turnover"), 0.0)
    queries = build_queries(
        company=client.company_name or "",
        as_at=as_at,
        playbook_ye=playbook_ye,
        lines=lines,
        buckets=dict(buckets),
        unknown=unknown,
        counts=result["counts"],
        notes=extra_notes,
    )

    root = client_root_path(client)
    wp = root / "Current" / "Working Papers"
    iris_dir = root / "Current" / "IRIS Import"
    wp.mkdir(parents=True, exist_ok=True)
    iris_dir.mkdir(parents=True, exist_ok=True)

    ye_words = as_at.strftime("%d %B %Y")
    safe = (client.company_name or "Client").strip()
    pack_path = wp / f"{safe} - {ye_words} - Draft accounts pack.xlsx"
    q_path = wp / f"{safe} - {ye_words} - Queries.md"
    iris_path = iris_dir / f"{as_at.isoformat()} IRIS Elements TB.csv"
    map_path = iris_dir / f"{as_at.isoformat()} Accology chart mapping.csv"

    net, iris_unknown = write_iris_csv(iris_path, ye_header(as_at), dict(buckets), names)
    with map_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["source", "section", "amount", "iris", "score", "status"]
        )
        w.writeheader()
        for ln in lines:
            w.writerow(
                {
                    "source": ln["source"],
                    "section": ln.get("section") or "",
                    "amount": f"{ln['amount']:.2f}",
                    "iris": ln["iris"],
                    "score": f"{ln['score']:.2f}",
                    "status": "mapped" if ln["iris"] in names else "unmapped",
                }
            )

    write_pack(
        pack_path,
        company=client.company_name or "",
        as_at=as_at,
        tenant_name=result["tenant"],
        source_files=result["pulled"],
        lines=lines,
        buckets=dict(buckets),
        queries=queries,
        cover_stats=cover_stats,
        pl_lines=pl_lines,
        bs_sections=bs_sections,
    )
    write_queries_md(q_path, client.company_name or "", as_at, queries, result["pulled"], net)

    result.update(
        {
            "ok": True,
            "net": net,
            "unknown": unknown or iris_unknown,
            "queries": queries,
            "pack": str(pack_path),
            "iris": str(iris_path),
            "queries_md": str(q_path),
            "mapping": str(map_path),
            "turnover": turnover,
            "pbt": pbt,
            "net_assets": net_assets,
            "tb_lines": len(lines),
        }
    )
    return result


JOBS = [
    # client_id, as_at, extra notes
    (
        1,
        date(2025, 12, 31),
        [
            "Existing working papers in Current/Working Papers are labelled 31 December 2025; "
            "playbook / AGENTS.md say 28/12. Pulled 31 December to match those papers."
        ],
    ),
    (103, date(2025, 12, 31), []),
    (42, date(2026, 3, 31), []),
    (55, date(2026, 3, 31), []),
    (97, date(2026, 6, 29), []),
    (66, date(2026, 7, 30), []),
    (81, date(2026, 7, 31), []),
    (116, date(2026, 7, 31), []),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", type=int, required=True)
    args = parser.parse_args()
    spec = next((j for j in JOBS if j[0] == args.client_id), None)
    if not spec:
        print(f"Client {args.client_id} is not one of the eight year-end jobs.")
        return 2
    cid, as_at, notes = spec
    db = SessionLocal()
    try:
        client = db.get(Client, cid)
        if not client:
            print(f"No client id {cid} in local crm.db")
            return 2
        pb = get_or_create_playbook(db, client.id)
        playbook_ye = None
        if pb.year_end_month:
            y = as_at.year
            m = int(pb.year_end_month)
            d = int(pb.year_end_day or monthrange(y, m)[1])
            d = min(d, monthrange(y, m)[1])
            playbook_ye = date(y, m, d)
        print(f"=== {client.company_name} id={client.id} as_at={as_at} ===")
        result = process_client(db, client, as_at, playbook_ye, notes)
        print(json.dumps({k: v for k, v in result.items() if k != "queries"}, indent=2, default=str))
        print("--- queries ---")
        for i, q in enumerate(result.get("queries") or [], 1):
            print(f"{i}. {q}")
        if not result.get("ok"):
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
