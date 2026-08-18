"""Second pass: file leftover Unmatched + Desktop JMG packs.

Uses CRM current vs inactive, plus typo aliases. Named companies/people
not in the CRM go to Lost Clients. Practice admin stays under Practice.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"C:\Users\User\accountant-crm")
os.chdir(r"C:\Users\User\accountant-crm")

from scripts.organise_practice_files import (  # noqa: E402
    CLIENTS_DIR,
    LOST_DIR,
    TREE,
    UNMATCHED,
    dest_for,
    guess_category,
    load_clients,
    match_client,
    sanitize,
    sha256_file,
    unique_dest,
)

PRACTICE = TREE / "Practice"
LOG = Path(r"C:\Users\User\OneDrive - Accology\Documents") / (
    f"FILE-ORGANISE-PASS2-{datetime.now():%Y%m%d-%H%M}.txt"
)
DESKTOP = Path(r"C:\Users\User\OneDrive - Accology\Desktop")

ALIASES = {
    "alan fennel": "A Fenn Limited",
    "alan fennell": "Mr Alan Fennell",
    "ane campbell": "Mrs Anne Campbell",
    "annec campbell": "Mrs Anne Campbell",
    "anne campbell": "Mrs Anne Campbell",
    "angula booth": "Miss Anjula Booth",
    "anula booth": "Miss Anjula Booth",
    "anjula booth": "Miss Anjula Booth",
    "ashton photographic": "Ashton Photographics Limited",
    "buzzcommunications": "BUZZ COMMUNICATIONS LIMITED",
    "buzz communications": "BUZZ COMMUNICATIONS LIMITED",
    "centralprosthetic": "Central Prosthetics",
    "central prosthetic": "Central Prosthetics",
    "church viw": "Church View Engineering Limited",
    "church view": "Church View Engineering Limited",
    "computer care comsultants": "Computer Care Consultants Limited",
    "computer care consultants": "Computer Care Consultants Limited",
    "dans tax": "Dans Tax Ltd",
    "dan s tax": "Dans Tax Ltd",
    "d graham": "Mr David Graham",
    "david graham": "Mr David Graham",
    "david kelly": "Accology Limited",
    "empire warwick": "Empire Luxury Homes Ltd",
    "forshaws fensing": "Forshaws Fencing Limited",
    "forshaws fencing": "Forshaws Fencing Limited",
    "full dispaly": "Full Display Ltd",
    "full display": "Full Display Ltd",
    "gareth sahw": "Mr Gareth Shaw",
    "gareth shaw": "Mr Gareth Shaw",
    "go green peletting": "GO GREEN PELLETING SOLUTIONS LIMITED",
    "go green pelleting": "GO GREEN PELLETING SOLUTIONS LIMITED",
    "go green wood fuals": "Go Green Wood Fuels Limited",
    "go green wood fuels": "Go Green Wood Fuels Limited",
    "haus trns": "Haus Birmingham Ltd",
    "haus birmingham": "Haus Birmingham Ltd",
    "ian hudosn": "Mr Ian Hudson",
    "ian hudson": "Mr Ian Hudson",
    "ian james hudson": "Mr Ian Hudson",
    "jmg": "John Munroe Group Limited",
    "john munroe": "John Munroe Group Limited",
    "lancahire": "Lancashire & Midlands Stations Limited",
    "lancashire and midlds": "Lancashire & Midlands Stations Limited",
    "lancashire midlads": "Lancashire & Midlands Stations Limited",
    "lancashire midlands": "Lancashire & Midlands Stations Limited",
    "lee ottley": "Mr Lee Martin Ottley",
    "leee ottley": "Mr Lee Martin Ottley",
    "losh interirors": "LOSH INTERIORS NW LTD",
    "losh intriors": "LOSH INTERIORS NW LTD",
    "losh interiors": "LOSH INTERIORS NW LTD",
    "map railway": "M A P Railway Solutions Ltd",
    "m a p railway": "M A P Railway Solutions Ltd",
    "matt mills": "Mr Matthew Mills",
    "matthew mills": "Mr Matthew Mills",
    "matt mils": "Mr Matthew Mills",
    "matthew kenny": "Mr Mark Kenny",
    "max bevan": "Mr Max Darrigan - Bevan",
    "melissa farrel": "Miss Melissa Leanne Farrell",
    "melissa farrell": "Miss Melissa Leanne Farrell",
    "my map to freedon": "My Map To Freedom Ltd",
    "my map to freedom": "My Map To Freedom Ltd",
    "outlooks propety chester": "Outlooks Property Chester Central Limited",
    "outlooks property chester": "Outlooks Property Chester Central Limited",
    "parker clby": "Parker Colby Insurance Brokers Ltd",
    "parker colby": "Parker Colby Insurance Brokers Ltd",
    "phillip williams": "Mr Philip James Williams",
    "philip williams": "Mr Philip James Williams",
    "quiktrak": "QUICKTRAK LTD",
    "quicktrak": "QUICKTRAK LTD",
    "rachael neiill": "Mrs Rachael Neill",
    "rachael neil": "Mrs Rachael Neill",
    "rachael neill": "Mrs Rachael Neill",
    "sixty six": "Sixty Six Interiors Ltd",
    "slok ceiling": "Slok Ceiling & Partition Solutions Ltd",
    "slok cps": "Slok Ceiling & Partition Solutions Ltd",
    "surface solutions pension": "Surface Solutions (Manchester) Limited",
    "surface solutions": "Surface Solutions (Manchester) Limited",
    "aj harriss": "Surface Solutions (Manchester) Limited",
    "araco limited": "Araco Interiors Limited",
    "araco interiors": "Araco Interiors Limited",
    "access utilities": "ACCESS UTILITIES (UK) LIMITED",
    "accology limited": "Accology Limited",
    "acccology": "Accology Limited",
    "accolgoy": "Accology Limited",
    "m a french": "Accology Limited",
    "multi trade desig": "Multi Trade Design & Build Ltd",
    "multi trade design": "Multi Trade Design & Build Ltd",
    "jc jones": "Jc Jones Will Trust",
    "jcg jones": "Jc Jones Will Trust",
    "trustees of jcg": "Jc Jones Will Trust",
    "thrive rawtenstall": "Thrive 24 Limited",
    "thrive 24": "Thrive 24 Limited",
    "warwick homes": "WARWICK HOMES DEVELOPMENT BUILD LIMITED",
    "wawick homes": "WARWICK HOMES DEVELOPMENT BUILD LIMITED",
    "m hurley": "Mr Michael Hurley",
    "michael hurley": "Mr Michael Hurley",
    "m fletcher": "Mr Mark Fletcher",
    "mark fletcher": "Mr Mark Fletcher",
    "p.j. barber": "Mr Philip Barber",
    "philip barber": "Mr Philip Barber",
    "k. barber": "Mrs Kathryn Barber",
    "kathryn barber": "Mrs Kathryn Barber",
    "phil petch": "Mr Philip Charles Petch",
    "kathryn jones": "Jc Jones Will Trust",
    "k a jones": "Jc Jones Will Trust",
    "tony marie jones": "Mrs Marie Jones",
    "marie jones": "Mrs Marie Jones",
    "old milton house": "Mrs Marie Jones",
    "mr m fletcher": "Mr Mark Fletcher",
}

# Named companies/people not in CRM → Lost Clients
LOST_ONLY = {
    "aqua babies": "Aqua Babies (UK) Limited",
    "aqua 123": "Aqua Babies (UK) Limited",
    "aramax": "Aramax Limited",
    "albion cooked meats": "Albion Cooked Meats Limited",
    "ashton photographic": "Ashton Photographics Limited",
    "berwyn house": "Berwyn House",
    "bluewates": "Bluewates",
    "td4 milkshakes": "TD4 Milkshakes Limited",
    "kmg limited": "KMG Limited",
    "nottingham f": "Nottingham F & B Limited",
    "sones financial": "Sones Financial Limited",
    "sones valley": "Sones Financial Limited",
    "zen living": "The Zen Living Group Limited",
    "matt pratt": "Matt Pratt",
    "mathew pratt": "Matt Pratt",
    "matthew pratt": "Matt Pratt",
    "angela watson pratt": "Angela Watson Pratt",
    "steve strickland": "Steve Strickland",
    "timothy haughton": "Timothy Haughton",
    "frank rafferty": "Frank Rafferty",
    "wendy edmondson": "Wendy Edmondson",
    "gymco reserve": "Gymco Reserve Limited",
}

PRACTICE_NEEDLES = (
    "acca 2020",
    "client service register",
    "companies house id",
    "companies status report",
    "companies table",
    "jobs table",
    "peoples table",
    "people table",
    "person company link",
    "services table",
    "mailing database",
    "all contacts",
    "accologise innovation",
    "work in progress",
    "wip -",
    "aged debtors",
    "assets-2025",
    "landmark clients",
    "id tax consideration",
    "editable id3",
    "vat1_editable",
    "nil vat return",
    "p11d workings 2024",
    "cash flow post corona",
    "model inputs and tables",
    "acquisition_assessment",
    "innovate uk",
    "gg id passwords",
    "visit confirmation",
    "file-organise-log",
    "newsqldata",
    "connect to new data",
    "newsqlserver",
)

JMG_AUDIT_PREFIXES = (
    "o0 ", "o1 ", "o3 ", "o4 ", "o5 ", "o6 ",
    "p0 ", "p1 ", "p2 ", "p3 ", "p4 ", "p5 ", "p6 ",
    "qa0 ", "qa1 ", "qa2 ", "qa3 ", "qa4 ", "qa5 ", "qa6 ",
    "qa7 ", "qa8 ", "qa9 ", "qa10 ", "qa11 ", "qa12 ",
    "qb0 ", "qb1 ", "qb2 ", "qb3 ", "qb4 ", "qb5 ", "qb6 ",
    "qb7 ", "qb8 ", "qb9 ",
    "qc0 ", "qc1 ", "qc2 ", "qc3 ", "qc4 ", "qc5 ", "qc6 ",
    "qc7 ", "qc8 ", "qc9 ", "qc10 ",
    "s0 ", "s1 ",
    "copy of j10",
)

LEAVE_UNMATCHED = {
    "img_0310.heic",
    "img_0311.heic",
    "doglogcabinplan 100 by 50.xlsx",
    "ella car.xlsx",
    "bolton rufc",
    "iexplorer.appref-ms",
    "att00002.htm",
    "script 1.osts",
    "chatbot_",
    "yodel loss",
    "vote by correspondence",
    "isurance breakdown",
    "spends 6 months",
    "account-statement",
    "transaction-statement",
    "payments-2025",
    "monzo statement",
    "statements09012972468687",
    "e h smith",
    "emails whb",
}


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def apply_aliases(clients) -> None:
    by = {}
    for c in clients:
        for n in c["names"]:
            by[n] = c
        by[norm(c["slug"])] = c
        by[norm(c["name"])] = c
    for alias, target in ALIASES.items():
        hit = None
        t = norm(target)
        for c in clients:
            if t in c["names"] or t == norm(c["slug"]) or t == norm(c["name"]):
                hit = c
                break
            if target.lower() in (c["slug"] or "").lower():
                hit = c
                break
        if hit:
            hit["names"].add(norm(alias))
        else:
            log(f"ALIAS MISS {alias!r} -> {target!r}")


def is_practice(name: str) -> bool:
    n = norm(name)
    return any(norm(x) in n for x in PRACTICE_NEEDLES)


def is_leave(name: str) -> bool:
    n = norm(name)
    return any(norm(x) in n for x in LEAVE_UNMATCHED)


def is_jmg_audit(name: str) -> bool:
    n = norm(name) + " "
    return any(n.startswith(p) or f" {p}" in f" {n}" for p in JMG_AUDIT_PREFIXES)


def lost_only_name(filename: str) -> str | None:
    n = norm(filename)
    best = None
    for needle, dest in LOST_ONLY.items():
        if norm(needle) in n:
            if best is None or len(needle) > len(best[0]):
                best = (needle, dest)
    return best[1] if best else None


def move_file(src: Path, dest_dir: Path, seen: dict) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        digest = sha256_file(src)
    except OSError as e:
        return f"ERR hash {src.name}: {e}"
    existing = seen.get(digest)
    if existing and existing.exists():
        try:
            src.unlink()
        except OSError:
            pass
        return f"DUP {src.name} == {existing}"
    dest = unique_dest(dest_dir, src.name)
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        return f"ERR move {src.name}: {e}"
    seen[digest] = dest
    return f"MOVE {src.name} -> {dest_dir.relative_to(TREE)}"


def move_tree(src: Path, dest_dir: Path, seen: dict) -> int:
    n = 0
    if src.is_file():
        log(move_file(src, dest_dir, seen))
        return 1
    for p in src.rglob("*"):
        if p.is_file():
            log(move_file(p, dest_dir, seen))
            n += 1
    try:
        shutil.rmtree(src, ignore_errors=True)
    except OSError:
        pass
    return n


def main() -> None:
    LOG.write_text("", encoding="utf-8")
    log(f"Pass 2 started {datetime.now().isoformat()}")
    clients = load_clients()
    apply_aliases(clients)
    jmg = next((c for c in clients if "john munroe" in norm(c["name"])), None)
    PRACTICE.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Path] = {}
    moved = 0

    # Desktop JMG packs
    for name in ("JMG Audit File 2019", "JMG2020"):
        p = DESKTOP / name
        if p.exists() and jmg:
            dest = dest_for(jmg, "Working Papers")
            log(f"DESKTOP PACK {name} -> {dest}")
            moved += move_tree(p, dest, seen)

    files = [p for p in UNMATCHED.iterdir() if p.is_file() or p.is_dir()]
    leftover = 0
    for src in files:
        name = src.name
        if is_leave(name):
            leftover += 1
            continue
        if is_practice(name):
            dest = PRACTICE / ("Working Papers" if src.is_dir() else guess_category(name))
            if src.is_dir():
                moved += move_tree(src, dest, seen)
            else:
                log(move_file(src, dest, seen))
                moved += 1
            continue
        if is_jmg_audit(name) and jmg:
            dest = dest_for(jmg, "Working Papers")
            if src.is_dir():
                moved += move_tree(src, dest, seen)
            else:
                log(move_file(src, dest, seen))
                moved += 1
            continue
        client = match_client(name, clients)
        if client:
            dest = dest_for(client, guess_category(name))
            if src.is_dir():
                moved += move_tree(src, dest, seen)
            else:
                log(move_file(src, dest, seen))
                moved += 1
            continue
        lost_name = lost_only_name(name)
        if lost_name:
            dest = LOST_DIR / sanitize(lost_name) / guess_category(name)
            if src.is_dir():
                moved += move_tree(src, dest, seen)
            else:
                log(move_file(src, dest, seen))
                moved += 1
            continue
        leftover += 1

    remaining = list(UNMATCHED.iterdir()) if UNMATCHED.exists() else []
    log(f"MOVED_OR_FILED={moved}")
    log(f"LEFT_UNMATCHED={len(remaining)}")
    log("REMAINING:")
    for p in remaining:
        log(f"  {p.name}")
    log(f"Pass 2 finished {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
