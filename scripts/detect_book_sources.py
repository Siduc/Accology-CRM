"""Detect Xero / Sage / QuickBooks from prior-year papers and update playbooks.

  python scripts/detect_book_sources.py
  python scripts/detect_book_sources.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models.client import Client
from app.services.client_playbook import (
    BOOKKEEPING_SOURCES,
    client_folder_name,
    ensure_client_pack,
    get_or_create_playbook,
    live_clients_query,
    practice_files_root,
    render_agents_md,
)

SOURCE_CODES = {c for c, _ in BOOKKEEPING_SOURCES}

HINTS = {
    "xero": (
        r"\bxero\b",
        r"organisationid",
        r"manual.?journal",
        r"trackingoption",
        r"banktransactionid",
    ),
    "sage50": (
        r"\bsage\s*50\b",
        r"\bsage50\b",
        r"sage.?line\s*50",
        r"nominal.?ledger.?sage",
        r"sage\.export",
    ),
    "sage_cloud": (
        r"sage\s*(one|business\s*cloud|accounting)",
        r"accounting\.sage\.com",
        r"sageone",
    ),
    "qbo": (
        r"quickbooks",
        r"\bqbo\b",
        r"qb\s*online",
        r"intuit",
        r"realmid",
        r"journalentry",
    ),
    "bank_csv": (
        r"natwest",
        r"barclays",
        r"hsbc",
        r"\bmonzo\b",
        r"starling",
        r"lloyds",
        r"bank.?statement",
        r"account.?statement",
    ),
}

# Filename-only extra weight
NAME_HINTS = {
    "xero": (r"xero",),
    "sage50": (r"sage\s*50", r"sage50"),
    "sage_cloud": (r"sage\s*one", r"sage.?cloud"),
    "qbo": (r"quickbook", r"\bqbo\b", r"qb[-_ ]?online"),
    "bank_csv": (r"natwest", r"barclays", r"monzo", r"starling", r"hsbc"),
}

SKIP_DIR = {".git", "__pycache__", "node_modules"}
TEXT_EXT = {".csv", ".txt", ".tsv", ".json", ".xml", ".html", ".htm"}
MAX_READ = 80_000


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def score_text(text: str) -> Dict[str, int]:
    t = _norm(text)
    scores: Dict[str, int] = defaultdict(int)
    for src, pats in HINTS.items():
        for pat in pats:
            hits = len(re.findall(pat, t, flags=re.I))
            if hits:
                scores[src] += min(hits, 8)
    return scores


def score_name(name: str) -> Dict[str, int]:
    t = _norm(name)
    scores: Dict[str, int] = defaultdict(int)
    for src, pats in NAME_HINTS.items():
        for pat in pats:
            if re.search(pat, t, flags=re.I):
                scores[src] += 6
    scores.update(score_text(t))
    return scores


def read_head(path: Path) -> str:
    try:
        raw = path.read_bytes()[:MAX_READ]
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


SCAN_DIRS = {
    "current",
    "working papers",
    "accounts",
    "tax return",
    "source",
    "iris import",
    "journals",
}


def walk_client_dir(folder: Path) -> Tuple[Dict[str, int], List[str]]:
    scores: Dict[str, int] = defaultdict(int)
    evidence: List[str] = []
    if not folder.is_dir():
        return scores, evidence
    targets: List[Path] = [folder]
    for child in folder.iterdir():
        if child.is_dir() and child.name.lower() in SCAN_DIRS:
            targets.append(child)
    seen = set()
    files: List[Path] = []
    for base in targets:
        try:
            iterator = base.rglob("*") if base != folder else base.glob("*")
        except OSError:
            continue
        for path in iterator:
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            files.append(path)
            if len(files) > 400:
                break
        if len(files) > 400:
            break
    for path in files:
        if any(p.lower() in SKIP_DIR for p in path.parts):
            continue
        if path.name.startswith("~$") or path.name.lower() == "agents.md":
            continue
        rel = str(path.relative_to(folder))
        name_scores = score_name(rel)
        file_scores: Dict[str, int] = defaultdict(int, name_scores)
        if path.suffix.lower() in TEXT_EXT and path.stat().st_size < 2_000_000:
            head = read_head(path)
            if head:
                for src, n in score_text(head[:8000]).items():
                    file_scores[src] += n
        if not file_scores:
            continue
        top_src, top_n = max(file_scores.items(), key=lambda kv: kv[1])
        if top_n < 4:
            continue
        for src, n in file_scores.items():
            scores[src] += n
        evidence.append(f"{top_src} +{top_n} · {rel}")
        if len(evidence) > 12:
            # keep going for scores, trim evidence later
            pass
    return scores, evidence[:12]


def pick_source(scores: Dict[str, int], crm_hint: str = "") -> Tuple[str, str]:
    if not scores:
        if crm_hint in SOURCE_CODES and crm_hint != "xero":
            return crm_hint, "crm field"
        return "xero", "default (no paper evidence — practice is mostly Xero)"
    # Sage 50 vs cloud: if both, prefer the higher; tie → sage50 (UK practices)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, best_n = ranked[0]
    second_n = ranked[1][1] if len(ranked) > 1 else 0
    if best == "bank_csv" and any(s in scores and scores[s] >= 4 for s in ("xero", "sage50", "sage_cloud", "qbo")):
        # bank files often sit next to real books
        for src, n in ranked:
            if src != "bank_csv":
                best, best_n = src, n
                break
    note = f"papers score {dict(scores)}"
    if best_n < 4:
        return "xero", f"weak evidence {note} — left as Xero default"
    if second_n and second_n >= best_n * 0.7 and ranked[1][0] != best:
        note += f"; also {ranked[1][0]}={second_n}"
    return best, note


def crm_hint(client: Client) -> str:
    blob = " ".join(
        str(x or "")
        for x in (
            client.accounts_software_id,
            client.xero_username,
            client.notes,
        )
    ).lower()
    if "quickbook" in blob or "qbo" in blob:
        return "qbo"
    if "sage 50" in blob or "sage50" in blob:
        return "sage50"
    if "sage" in blob:
        return "sage_cloud"
    if "xero" in blob:
        return "xero"
    if (client.xero_username or client.accounts_software_id or "").strip():
        return "xero"
    return ""


def detect_all(db, *, dry_run: bool) -> List[dict]:
    root = practice_files_root() / "Clients"
    rows = []
    clients = live_clients_query(db).order_by(Client.company_name).all()
    for client in clients:
        folder = root / client_folder_name(client)
        scores, evidence = walk_client_dir(folder)
        hint = crm_hint(client)
        if hint:
            scores[hint] = scores.get(hint, 0) + 3
        source, why = pick_source(scores, hint)
        pb = get_or_create_playbook(db, client.id)
        prev = pb.bookkeeping_source or "xero"
        changed = prev != source
        snippet = f"Source detected from working papers: {source}. {why}."
        if not dry_run:
            pb.bookkeeping_source = source
            existing = (pb.source_notes or "").strip()
            if "Source detected from working papers" in existing:
                existing = re.sub(
                    r"Source detected from working papers:.*?(?=\n|$)",
                    snippet.rstrip("."),
                    existing,
                    count=1,
                    flags=re.I,
                )
                pb.source_notes = existing.strip()
            else:
                pb.source_notes = f"{snippet} {existing}".strip() if existing else snippet
            ensure_client_pack(db, client, playbook=pb, move_prior_years=False, commit=False)
            # rewrite AGENTS.md with new source
            md = folder / "AGENTS.md"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                md.write_text(render_agents_md(db, client, pb), encoding="utf-8")
            except OSError:
                pass
        rows.append(
            {
                "id": client.id,
                "name": client.company_name,
                "source": source,
                "previous": prev,
                "changed": changed,
                "why": why,
                "scores": dict(scores),
                "evidence": evidence[:6],
                "folder": str(folder) if folder.is_dir() else "",
            }
        )
    if not dry_run:
        db.commit()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        rows = detect_all(db, dry_run=args.dry_run)
    finally:
        db.close()
    counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["source"]] += 1
    out = ROOT / "docs" / "book-source-detection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"counts": dict(counts), "clients": rows}, indent=2), encoding="utf-8")
    print(f"{'DRY RUN ' if args.dry_run else ''}Clients {len(rows)}  counts {dict(counts)}")
    print(f"Wrote {out}")
    for src, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {src:12} {n}")
    mixed = [r for r in rows if "also " in (r.get("why") or "")]
    if mixed:
        print(f"Mixed/uncertain: {len(mixed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
