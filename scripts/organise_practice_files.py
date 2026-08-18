"""
One-off: file loose practice documents into Accologise Documents
per CRM layout, split current vs lost clients, drop exact duplicates.

  Accologise Documents / Clients / {Client} / {Category}
  Accologise Documents / Lost Clients / {Client} / {Category}
  Accologise Documents / Unmatched / Needs review

Does not delete from the Seagate. Moves within OneDrive - Accology.
Keeps the newest copy of SHA-256 duplicates.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\Users\User\OneDrive - Accology")
TREE = ROOT / "Accologise Documents"
CLIENTS_DIR = TREE / "Clients"
LOST_DIR = TREE / "Lost Clients"
UNMATCHED = TREE / "Unmatched" / "Needs review"
LOG = ROOT / "Documents" / f"FILE-ORGANISE-LOG-{datetime.now():%Y%m%d-%H%M}.txt"

# Sources to harvest (Seagate is copy-only, never delete)
MOVE_SOURCES = [
    ROOT / "Desktop",
    ROOT / "Documents",
    ROOT,  # loose files at OneDrive root
]
COPY_SOURCES = [
    Path(r"D:\OUR-WORK-FOR-NEW-LAPTOP"),
]
SKIP_DIR_NAMES = {
    "accologise documents",
    "accologise",
    "accologise post",
    "accologies post",
    "apps",
    "pictures",
    "videos",
    "meetings",
    "notebooks",
    "recordings",
    "shared with everyone",
    "microsoft teams chat files",
    "microsoft copilot chat files",
    "office lens",
    "outlook",
    "outlook customer manager",
    "attachments",
    "email attachments",
    "xero imports",
    "master accology",
    ".git",
}
SKIP_EXT = {".lnk", ".url", ".exe", ".dll", ".ini", ".tmp", ".lock"}
SKIP_PREFIX = ("~$", ".")

CATEGORIES = [
    ("engagement letter", "Engagement Letter"),
    ("professional clearance", "Correspondence"),
    ("clearance letter", "Correspondence"),
    ("working paper", "Working Papers"),
    ("working papers", "Working Papers"),
    ("tax return", "Tax Return"),
    ("sar working", "Tax Return"),
    ("sar workings", "Tax Return"),
    ("ct600", "Tax Return"),
    ("ct computation", "Tax Return"),
    ("p11d", "Tax Return"),
    ("vat return", "Working Papers"),
    ("vat cert", "Working Papers"),
    ("invoice", "Invoices"),
    ("invocie", "Invoices"),
    ("invocies", "Invoices"),
    ("proposal", "Proposals"),
    ("projection", "Working Papers"),
    ("accounts", "Accounts"),
    ("trial balance", "Working Papers"),
    ("bank statement", "Working Papers"),
    ("kyc", "ID-KYC"),
    ("passport", "ID-KYC"),
    ("id3", "ID-KYC"),
]


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def sanitize(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "-", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120] or "Untitled"


def norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_entity(s: str) -> str:
    s = norm(s)
    for w in (
        "limited",
        "ltd",
        "llp",
        "llc",
        "plc",
        "group",
        "holdings",
        "company",
        "ta",
        "t a",
        "trading as",
    ):
        s = re.sub(rf"\b{w}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def guess_category(filename: str) -> str:
    n = norm(filename)
    for needle, cat in CATEGORIES:
        if needle in n:
            return cat
    return "Working Papers"


def sha256_file(path: Path, limit: int = 0) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        if limit:
            h.update(f.read(limit))
        else:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def load_clients():
    sys.path.insert(0, r"C:\Users\User\accountant-crm")
    os.chdir(r"C:\Users\User\accountant-crm")
    from app.database import SessionLocal
    from app.models.client import Client
    from app.services.ms_graph_drive import client_folder_slug

    db = SessionLocal()
    rows = db.query(Client).all()
    clients = []
    for c in rows:
        try:
            slug = client_folder_slug(c)
        except Exception:
            slug = sanitize(c.company_name or f"Client {c.id}")
        status = (c.overall_status or "Active").strip()
        bucket = "lost" if status.lower() in ("inactive", "former", "lost") else "current"
        names = {norm(c.company_name or ""), strip_entity(c.company_name or "")}
        try:
            names.add(norm(c.display_name()))
            names.add(strip_entity(c.display_name()))
        except Exception:
            pass
        names.discard("")
        clients.append(
            {
                "id": c.id,
                "name": c.company_name or slug,
                "slug": slug,
                "status": status,
                "bucket": bucket,
                "names": names,
                "is_person": (c.client_type or "").lower() == "individual"
                or (c.company_number or "").upper().startswith("IND-"),
            }
        )
    db.close()
    # Aliases for messy filenames
    extra = {
        "ahmed bros": "Ahmed Brothers Partnership",
        "ahmed brothers": "Ahmed Brothers Partnership",
        "ahmedbros": "Ahmed Brothers Partnership",
        "slok ceiings": "Slok Ceiling & Partition Solutions Ltd",
        "slok ceilings": "Slok Ceiling & Partition Solutions Ltd",
        "sdh dry lining": "Sdh Drylining Ltd",
        "sdh drylining": "Sdh Drylining Ltd",
        "sixty six interiors": "Sixty Six Interiors Ltd",
        "66 interiors": "Sixty Six Interiors Ltd",
        "a fenn": "A Fenn Limited",
        "a. fenn": "A Fenn Limited",
        "structured outlooks": "STRUCTURED OUTLOOKS LTD",
        "outlooks accommodation": "Outlooks Accommodation Limited",
        "accology pays": "Accology Pays Limited",
        "accology limited": "Accology Limited",
        "accology lmited": "Accology Limited",
        "buzz communications": "BUZZ COMMUNICATIONS LIMITED",
        "bevan electricals": "Bevan Electricals Limited",
        "bevan electriclas": "Bevan Electricals Limited",
        "john wright": "Mr John James Wright",
    }
    by_slug = {c["slug"].lower(): c for c in clients}
    by_name = {}
    for c in clients:
        for n in c["names"]:
            by_name[n] = c
    for alias, target in extra.items():
        hit = None
        t = norm(target)
        for c in clients:
            if t in c["names"] or strip_entity(target) in c["names"] or target.lower() in c["slug"].lower():
                hit = c
                break
        if hit:
            hit["names"].add(norm(alias))
            hit["names"].add(strip_entity(alias))
    return clients


def match_client(filename: str, clients) -> dict | None:
    n = norm(filename)
    if not n:
        return None
    hits = []
    for c in clients:
        for token in c["names"]:
            if len(token) < 4:
                continue
            if token in n:
                # score: longer token, companies beat people
                score = len(token) * 10
                if not c["is_person"]:
                    score += 40
                if c["bucket"] == "current":
                    score += 5
                hits.append((score, c))
    if not hits:
        return None
    hits.sort(key=lambda x: x[0], reverse=True)
    # require a reasonably specific match
    if hits[0][0] < 50:
        return None
    return hits[0][1]


def dest_for(client: dict, category: str) -> Path:
    root = LOST_DIR if client["bucket"] == "lost" else CLIENTS_DIR
    return root / client["slug"] / category


def should_skip_dir(path: Path) -> bool:
    name = path.name.lower()
    if name in SKIP_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    # already in the organised tree
    try:
        path.resolve().relative_to(TREE.resolve())
        return True
    except ValueError:
        return False


def iter_files(root: Path, *, recurse_dirs: bool) -> list[Path]:
    out = []
    if not root.exists():
        return out
    if root.is_file():
        return [root]
    for p in root.iterdir():
        if p.name.startswith(".") or p.name.startswith("~$"):
            continue
        if p.is_dir():
            if not recurse_dirs or should_skip_dir(p):
                continue
            # special: Ahmed Bros invoices folder — treat as one client dump
            if "ahmed" in p.name.lower() and "invo" in p.name.lower():
                out.extend(p.rglob("*"))
                continue
            # Desktop subfolders (JMG Audit etc) — include
            out.extend(q for q in p.rglob("*") if q.is_file())
        elif p.is_file():
            out.append(p)
    return [f for f in out if f.is_file()]


def unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    if not dest.exists():
        return dest
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = folder / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def main() -> None:
    LOG.write_text("", encoding="utf-8")
    log(f"Organise started {datetime.now().isoformat()}")
    clients = load_clients()
    current = [c for c in clients if c["bucket"] == "current"]
    lost = [c for c in clients if c["bucket"] == "lost"]
    log(f"CRM clients: {len(current)} current, {len(lost)} lost")

    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOST_DIR.mkdir(parents=True, exist_ok=True)
    UNMATCHED.mkdir(parents=True, exist_ok=True)

    # Seed empty current / lost folders (CRM scan expects Clients / Name)
    for c in current:
        (CLIENTS_DIR / c["slug"] / "Working Papers").mkdir(parents=True, exist_ok=True)
    for c in lost:
        (LOST_DIR / c["slug"] / "Working Papers").mkdir(parents=True, exist_ok=True)
    log(f"Created folder shells under {CLIENTS_DIR} and {LOST_DIR}")

    # Merge legacy Accologise Documents\Clients already there — leave in place
    # (they already match CRM names). We'll still file NEW loose files.

    seen_hash: dict[str, tuple[Path, float]] = {}
    moved = copied = skipped_dup = unmatched = 0
    errors = 0

    files: list[tuple[Path, bool]] = []  # path, can_delete_source
    for src in MOVE_SOURCES:
        if src.resolve() == ROOT.resolve():
            for p in src.iterdir():
                if p.is_file():
                    files.append((p, True))
            continue
        if src.name.lower() == "documents":
            for p in src.iterdir():
                if p.name.lower() in ("accologise post", "accologies post"):
                    continue
                if p.is_file():
                    files.append((p, True))
                elif p.is_dir() and not should_skip_dir(p):
                    for f in p.rglob("*"):
                        if f.is_file():
                            files.append((f, True))
            continue
        if src.name.lower() == "desktop":
            for f in iter_files(src, recurse_dirs=True):
                files.append((f, True))

    # Seagate: only OUR-WORK leftover bits not already here (skip if dest exists)
    our = Path(r"D:\OUR-WORK-FOR-NEW-LAPTOP")
    if our.exists():
        skip_roots = {"accountant-crm", "grok-config-and-chats"}
        for p in our.rglob("*"):
            if not p.is_file():
                continue
            parts = {x.lower() for x in p.relative_to(our).parts}
            if parts & skip_roots:
                continue
            if p.suffix.lower() in {".txt", ".md"}:
                continue
            files.append((p, False))

    log(f"Candidates: {len(files)}")

    for path, can_delete in files:
        ext = path.suffix.lower()
        if ext in SKIP_EXT or path.name.startswith(SKIP_PREFIX):
            continue
        if path.name.lower() in {"desktop.ini", "thumbs.db"}:
            continue
        try:
            rel_check = path.resolve()
            rel_check.relative_to(TREE.resolve())
            # already in organised tree
            continue
        except Exception:
            pass

        try:
            st = path.stat()
        except OSError:
            errors += 1
            continue

        client = match_client(path.name, clients)
        if not client:
            # also try parent folder name
            client = match_client(path.parent.name, clients)
        category = guess_category(path.name)
        if client:
            dest_dir = dest_for(client, category)
        else:
            dest_dir = UNMATCHED
            unmatched += 1
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            digest = sha256_file(path)
        except OSError:
            errors += 1
            continue

        if digest in seen_hash:
            prev, prev_mtime = seen_hash[digest]
            if st.st_mtime <= prev_mtime:
                skipped_dup += 1
                log(f"DUP-SKIP older {path}  (kept {prev})")
                if can_delete:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                continue
            # this one is newer — replace previous dest if it is a copy we made
            skipped_dup += 1
            log(f"DUP-NEWER {path} replaces {prev}")

        dest = dest_dir / path.name
        if dest.exists():
            try:
                if sha256_file(dest) == digest:
                    skipped_dup += 1
                    if can_delete and dest.stat().st_mtime >= st.st_mtime:
                        path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            dest = unique_dest(dest_dir, path.name)

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if can_delete:
                shutil.move(str(path), str(dest))
                moved += 1
                action = "MOVE"
            else:
                shutil.copy2(str(path), str(dest))
                copied += 1
                action = "COPY"
            seen_hash[digest] = (dest, dest.stat().st_mtime)
            who = client["slug"] if client else "UNMATCHED"
            log(f"{action} [{who}/{category}] {path.name}")
        except OSError as e:
            errors += 1
            log(f"ERROR {path} -> {dest}: {e}")

    log("")
    log(f"Moved {moved}, copied {copied}, dups removed/skipped {skipped_dup}, unmatched {unmatched}, errors {errors}")
    log(f"Log: {LOG}")


if __name__ == "__main__":
    main()
