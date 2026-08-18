"""Second pass: read Excel shared strings for Xero / Sage / QuickBooks."""

from __future__ import annotations

import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models.client import Client
from app.services.client_playbook import (
    client_folder_name,
    get_or_create_playbook,
    live_clients_query,
    practice_files_root,
    render_agents_md,
)

PATS = {
    "xero": re.compile(r"\bxero\b", re.I),
    "sage50": re.compile(r"sage\s*50|sage50", re.I),
    "sage_cloud": re.compile(r"sage\s*(one|business|cloud)|\bsage\b", re.I),
    "qbo": re.compile(r"quickbooks|\bqbo\b|qb\s*online|\bintuit\b", re.I),
}

XL = {".xlsx", ".xlsm", ".xlsb"}


def score_xlsx(path: Path) -> dict:
    scores = defaultdict(int)
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            blobs = []
            for n in names:
                low = n.lower()
                if low.endswith("sharedstrings.xml") or low.endswith("workbook.xml") or "sheet1" in low:
                    try:
                        blobs.append(zf.read(n)[:200_000].decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
            text = "\n".join(blobs)
    except Exception:
        return scores
    for src, pat in PATS.items():
        hits = len(pat.findall(text))
        if hits:
            # sage_cloud \bsage\b is noisy — require 2 hits unless sage50/cloud phrase
            if src == "sage_cloud" and hits < 2 and not re.search(
                r"sage\s*(one|business|cloud)", text, re.I
            ):
                continue
            scores[src] += min(hits, 12)
    return scores


def main() -> int:
    init_db()
    db = SessionLocal()
    root = practice_files_root() / "Clients"
    counts = defaultdict(int)
    changed = []
    try:
        clients = live_clients_query(db).order_by(Client.company_name).all()
        for client in clients:
            folder = root / client_folder_name(client)
            scores = defaultdict(int)
            hits = []
            if folder.is_dir():
                files = []
                for sub in ("Current", "Working Papers", "Accounts"):
                    d = folder / sub
                    if d.is_dir():
                        files.extend(p for p in d.rglob("*") if p.suffix.lower() in XL)
                files.extend(p for p in folder.glob("*") if p.suffix.lower() in XL)
                for path in files[:40]:
                    sc = score_xlsx(path)
                    if not sc:
                        continue
                    for k, v in sc.items():
                        scores[k] += v
                    top = max(sc, key=sc.get)
                    hits.append(f"{top} · {path.name}")
            # Don't let generic sage beat xero unless stronger
            if scores.get("xero", 0) >= scores.get("sage_cloud", 0):
                scores.pop("sage_cloud", None) if scores.get("xero", 0) and scores.get("sage_cloud", 0) <= scores.get("xero", 0) else None
            source = None
            if scores:
                source = max(scores, key=scores.get)
                if scores[source] < 2:
                    source = None
            pb = get_or_create_playbook(db, client.id)
            if source:
                # Accology Limited bank_csv was a false positive — excel wins
                pb.bookkeeping_source = source
                note = f"Excel papers mention {source} ({', '.join(hits[:3])})."
                existing = pb.source_notes or ""
                if "Excel papers mention" not in existing:
                    pb.source_notes = f"{note} {existing}".strip()
                changed.append((client.company_name, source, hits[:3]))
            elif pb.bookkeeping_source == "bank_csv" and client.id == 23:
                pb.bookkeeping_source = "xero"
                changed.append((client.company_name, "xero (reverted bank_csv)", []))
            counts[pb.bookkeeping_source or "xero"] += 1
            md = folder / "AGENTS.md"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                md.write_text(render_agents_md(db, client, pb), encoding="utf-8")
            except OSError:
                pass
        db.commit()
    finally:
        db.close()
    print("counts", dict(counts))
    print("excel-confirmed", len(changed))
    for name, src, ev in changed[:40]:
        print(f"  {src:12} {name}  {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
