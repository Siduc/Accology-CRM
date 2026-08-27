"""Code bank lines onto Accology Chart (IRIS Elements) names from the description."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

# First matching keyword wins. Keywords are lowercase substrings.
_RULES: Sequence[Tuple[Tuple[str, ...], str]] = (
    (("receipt inv", "receipt —", "receipt -"), "Turnover - Fees"),
    (("accology pays",), "Administrative expenses - Employee costs - Wages and salaries"),
    (("melissa farrell", "melissa"), "Creditors less than 1 year - Directors' loans"),
    (("nat west", "natwest", "bank charge", "monthly fee"), "Administrative expenses - General - Bank charges"),
    (("grok", "x.ai", "openai", "chatgpt", "microsoft", "adobe", "dropbox", "google", "software"),
     "Administrative expenses - General - Software"),
    (("uber", "trainline", "tfl", "parking", "petrol", "shell", "bp ", "esso"),
     "Administrative expenses - Employee costs - Travel and subsistence"),
    (("tesco", "sainsbury", "asda", "aldi", "lidl", "co-op", "waitrose", "mcdonald", "kfc", "costa",
      "starbucks", "nando", "pizza", "peking", "restaurant", "hollywood bowl", "mottram"),
     "Administrative expenses - Employee costs - Travel and subsistence"),
    (("booking.com", "hotel", "premier inn", "travelodge", "airbnb"),
     "Administrative expenses - Employee costs - Travel and subsistence"),
    (("amazon",), "Administrative expenses - General - Sundry expenses"),
    (("bolton arena", "mygp", "clinic"), "Administrative expenses - Employee costs - Staff training and welfare"),
    (("hmrc", "vat"), "Creditors less than 1 year - Other taxes and social security"),
    (("companies house",), "Administrative expenses - Legal & professional - Other legal and professional"),
    (("sage", "xero", "intuit", "quickbooks"), "Administrative expenses - General - Software"),
    (("monzo", "starling", "transfer"), "Cash - Cash at bank and in hand"),
)

DEFAULT_EXPENSE = "Administrative expenses - General - Sundry expenses"
DEFAULT_INCOME = "Turnover - Fees"


def code_from_description(description: str, *, amount: float = 0.0) -> str:
    blob = f"{description or ''}".lower()
    for keys, name in _RULES:
        if any(k in blob for k in keys):
            return name
    if amount > 0.005:
        return DEFAULT_INCOME
    return DEFAULT_EXPENSE


def recode_transaction(description: str, amount: float, current: Optional[str] = None) -> str:
    if current and current not in ("xero", "manual", "import", "sales", ""):
        # Already an IRIS-style name or a kept nominal
        if " - " in (current or ""):
            return current
    return code_from_description(description, amount=amount)
