"""IRIS Elements / Taxfiler trial-balance export.

IRIS has no practice API. Official path: CSV with header
Account, Description, Year End dd/mm/YYYY
Credits negative, net 0. Names must match Accology Chart (IRIS names).

Never invent figures — only remaps names from a source TB already on file.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.models.client import Client
from app.services.client_playbook import (
    client_root_path,
    current_accounts_year,
    ensure_client_pack,
    get_or_create_playbook,
    practice_files_root,
)

SAFE = re.compile(r"[^a-z0-9]+")
SKIP_NAME = re.compile(
    r"(mapping|iris elements|accology chart|chart of accounts|profit and loss|balance sheet)",
    re.I,
)


def accology_chart_path() -> Path:
    root = practice_files_root()
    return root / "Practice" / "Working Papers" / "Accology Chart.xlsx"


def load_chart_names(path: Optional[Path] = None) -> List[str]:
    from openpyxl import load_workbook

    chart = path or accology_chart_path()
    if not chart.is_file():
        return []
    wb = load_workbook(chart, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        names: List[str] = []
        for i, row in enumerate(ws.iter_rows(min_col=1, max_col=1, values_only=True), 1):
            val = (row[0] or "").strip() if row else ""
            if i == 1 and val.upper() in {"IRIS", "ACCOUNT", "NAME"}:
                continue
            if val:
                names.append(val)
        return names
    finally:
        wb.close()


def _norm(s: str) -> str:
    return SAFE.sub(" ", (s or "").lower()).strip()


def map_name(source: str, names: Sequence[str]) -> Tuple[str, float]:
    """Best Accology Chart name for a source line. Score 1.0 = exact."""
    raw = (source or "").strip()
    if not raw:
        return "", 0.0
    if raw in names:
        return raw, 1.0
    key = _norm(raw)
    if not key:
        return raw, 0.0
    by_norm = {_norm(n): n for n in names}
    if key in by_norm:
        return by_norm[key], 1.0
    best = raw
    best_score = 0.0
    for n in names:
        nn = _norm(n)
        if not nn:
            continue
        if key in nn or nn in key:
            score = 0.86 if len(key) >= 8 else 0.78
        else:
            score = SequenceMatcher(None, key, nn).ratio()
        if score > best_score:
            best_score = score
            best = n
    if best_score < 0.72:
        return raw, best_score
    return best, best_score


def _money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("£", "").replace(",", "").replace("(", "-").replace(")", "")
    if not s or s in {"-", "–"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _header_map(headers: Sequence[str]) -> Dict[str, int]:
    idx: Dict[str, int] = {}
    for i, h in enumerate(headers):
        k = _norm(str(h or ""))
        if not k:
            continue
        if k in {"account", "account name", "name", "description"} and "account" not in idx:
            if k == "description" and "account" in idx:
                idx["description"] = i
            else:
                idx.setdefault("account", i)
        elif k in {"description", "account description"}:
            idx.setdefault("description", i)
        elif k in {"debit", "debits", "dr"}:
            idx.setdefault("debit", i)
        elif k in {"credit", "credits", "cr"}:
            idx.setdefault("credit", i)
        elif k in {"amount", "balance", "ytd", "signed", "net"}:
            idx.setdefault("amount", i)
        elif "year end" in k or k.startswith("ye "):
            idx.setdefault("amount", i)
    return idx


def parse_tb_rows(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[Dict[str, Any]]:
    idx = _header_map(headers)
    if "account" not in idx:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        cells = list(row)
        acc = str(cells[idx["account"]] or "").strip() if idx["account"] < len(cells) else ""
        if not acc or acc.lower() in {"account", "total", "net", "net (must be 0.00)"}:
            continue
        debit = _money(cells[idx["debit"]]) if "debit" in idx and idx["debit"] < len(cells) else None
        credit = _money(cells[idx["credit"]]) if "credit" in idx and idx["credit"] < len(cells) else None
        amount = _money(cells[idx["amount"]]) if "amount" in idx and idx["amount"] < len(cells) else None
        if debit is None and credit is None and amount is None:
            continue
        if amount is None:
            amount = round((debit or 0.0) - (credit or 0.0), 2)
        if abs(amount) < 0.005:
            continue
        desc = ""
        if "description" in idx and idx["description"] < len(cells):
            desc = str(cells[idx["description"]] or "").strip()
        out.append({"source": acc, "description": desc or acc, "amount": round(amount, 2)})
    return out


def parse_loose_sheet(rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Accountant TB: account names in one column, debit/credit further right, no header."""
    if not rows:
        return []
    width = max((len(r) for r in rows), default=0)
    if width < 2:
        return []
    text_counts = [0] * width
    num_counts = [0] * width
    for row in rows:
        for i in range(min(width, len(row))):
            val = row[i]
            if val is None or str(val).strip() == "":
                continue
            if _money(val) is not None and not isinstance(val, str):
                num_counts[i] += 1
            elif _money(val) is not None and str(val).strip()[:1].isdigit():
                num_counts[i] += 1
            elif isinstance(val, str) and len(val.strip()) > 2:
                text_counts[i] += 1
    if not any(text_counts) or not any(num_counts):
        return []
    acc_col = max(range(width), key=lambda i: text_counts[i])
    num_cols = [i for i, n in enumerate(num_counts) if n >= 3 and i != acc_col]
    if not num_cols:
        return []
    num_cols.sort()
    debit_col = num_cols[0]
    credit_col = num_cols[1] if len(num_cols) > 1 else None
    out: List[Dict[str, Any]] = []
    skip = {"trial balance", "year ended", "account", "description", "total", "net"}
    for row in rows:
        if acc_col >= len(row):
            continue
        acc = str(row[acc_col] or "").strip()
        if not acc or acc.lower() in skip or len(acc) < 3:
            continue
        debit = _money(row[debit_col]) if debit_col < len(row) else None
        credit = (
            _money(row[credit_col]) if credit_col is not None and credit_col < len(row) else None
        )
        if debit is None and credit is None:
            continue
        amount = round((debit or 0.0) - (credit or 0.0), 2)
        if abs(amount) < 0.005:
            continue
        out.append({"source": acc, "description": acc, "amount": amount})
    return out


def parse_tb_file(path: Path) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return []
        parsed = parse_tb_rows(rows[0], rows[1:])
        return parsed or parse_loose_sheet(rows)
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        # data_only misses uncached formulas; raw values keep typed numbers.
        wb = load_workbook(path, read_only=True, data_only=False)
        try:
            sheets = list(wb.sheetnames)
            best: List[Dict[str, Any]] = []
            for name in sheets:
                rows = list(wb[name].iter_rows(values_only=True))
                if not rows:
                    continue
                parsed = parse_tb_rows([str(c or "") for c in rows[0]], rows[1:])
                if len(parsed) < 3:
                    parsed = parse_loose_sheet(rows)
                if len(parsed) > len(best):
                    best = parsed
        finally:
            wb.close()
        return best
    return []


def find_source_tb(source_dir: Path) -> Optional[Path]:
    if not source_dir.is_dir():
        return None
    candidates: List[Path] = []
    for p in source_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".csv", ".xlsx", ".xlsm"}:
            continue
        if p.name.startswith("~$"):
            continue
        if SKIP_NAME.search(p.name) and "trial" not in p.name.lower():
            continue
        candidates.append(p)
    if not candidates:
        return None

    def score(p: Path) -> Tuple[int, float]:
        n = p.name.lower()
        s = 0
        if "trial balance" in n or n.endswith(" tb.csv") or "trial_balance" in n:
            s += 5
        if "trial" in n:
            s += 3
        if p.suffix.lower() == ".csv":
            s += 1
        return (s, p.stat().st_mtime)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def ye_header(as_at: date) -> str:
    return f"Year End {as_at.strftime('%d/%m/%Y')}"


def write_iris_csv(path: Path, header: str, buckets: Dict[str, float], names: Sequence[str]) -> Tuple[float, List[str]]:
    unknown = [a for a in buckets if a not in names and abs(buckets[a]) >= 0.005]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Account", "Description", header])
        for account in sorted(buckets):
            amt = round(buckets[account], 2)
            if abs(amt) < 0.005:
                continue
            w.writerow([account, account, f"{amt:.2f}"])
        net = round(sum(buckets.values()), 2)
        w.writerow(["Net (must be 0.00)", "", f"{net:.2f}"])
    return net, unknown


@dataclass
class IrisExportResult:
    ok: bool
    error: str = ""
    source_file: str = ""
    iris_file: str = ""
    mapping_file: str = ""
    rows: int = 0
    net: float = 0.0
    unknown: List[str] = field(default_factory=list)
    mapped: int = 0


def export_client_iris(
    db: Session,
    client: Client,
    *,
    source_path: Optional[Path] = None,
    as_at: Optional[date] = None,
) -> IrisExportResult:
    pb = get_or_create_playbook(db, client.id)
    root = client_root_path(client)
    if not root:
        return IrisExportResult(ok=False, error="No client folder on this laptop’s OneDrive.")
    ensure_client_pack(db, client)
    current = root / "Current"
    source_dir = current / "Source"
    iris_dir = current / "IRIS Import"
    iris_dir.mkdir(parents=True, exist_ok=True)

    names = load_chart_names()
    if not names:
        return IrisExportResult(
            ok=False,
            error=f"Accology Chart not found at {accology_chart_path()}",
        )

    src = Path(source_path) if source_path else find_source_tb(source_dir)
    if not src or not src.is_file():
        return IrisExportResult(
            ok=False,
            error="No trial balance in Current/Source. Pull books or drop a TB CSV first.",
        )

    lines = parse_tb_file(src)
    if not lines:
        return IrisExportResult(
            ok=False,
            error=f"Could not read account/amount columns from {src.name}.",
            source_file=str(src),
        )

    year = current_accounts_year(db, client, pb)
    month = int(pb.year_end_month or 12)
    day = int(pb.year_end_day or 31)
    from calendar import monthrange

    month = min(12, max(1, month))
    last = monthrange(year, month)[1]
    ye = as_at or date(year, month, min(max(1, day), last))

    buckets: Dict[str, float] = defaultdict(float)
    mapping_rows: List[Dict[str, Any]] = []
    mapped = 0
    for line in lines:
        dest, score = map_name(line["source"], names)
        if dest in names:
            mapped += 1
        buckets[dest] = round(buckets[dest] + line["amount"], 2)
        mapping_rows.append(
            {
                "source": line["source"],
                "iris": dest,
                "amount": f"{line['amount']:.2f}",
                "score": f"{score:.2f}",
                "status": "mapped" if dest in names else "unmapped",
            }
        )

    stamp = ye.isoformat()
    iris_path = iris_dir / f"{stamp} IRIS Elements TB.csv"
    map_path = iris_dir / f"{stamp} Accology chart mapping.csv"
    if iris_path.exists():
        iris_path = iris_dir / f"{stamp} IRIS Elements TB auto.csv"
        map_path = iris_dir / f"{stamp} Accology chart mapping auto.csv"
    net, unknown = write_iris_csv(iris_path, ye_header(ye), dict(buckets), names)
    with map_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "iris", "amount", "score", "status"])
        w.writeheader()
        w.writerows(mapping_rows)

    return IrisExportResult(
        ok=True,
        source_file=str(src),
        iris_file=str(iris_path),
        mapping_file=str(map_path),
        rows=len(lines),
        net=net,
        unknown=unknown,
        mapped=mapped,
    )
