"""Display-only text helpers (no database writes)."""

import re

# Leading honorifics stripped from people names so list sort is by surname/forename
_HONORIFICS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mx",
        "dr",
        "prof",
        "professor",
        "sir",
        "lady",
        "lord",
        "dame",
        "rev",
        "reverend",
        "master",
        "capt",
        "captain",
        "col",
        "colonel",
        "maj",
        "major",
        "lt",
        "lieutenant",
        "sgt",
        "sergeant",
    }
)


def strip_name_titles(value) -> str:
    """
    Drop leading Mr / Mrs / Ms / Dr etc. (with optional trailing full stop).
    Display only — does not change the database.
    """
    if value is None or value == "":
        return "" if value == "" else ""
    s = str(value).strip()
    if not s or s == "—":
        return s
    # Repeat in case of "Mr. Dr. Smith" oddities (usually once is enough)
    for _ in range(3):
        m = re.match(r"^([A-Za-z]+)\.?\s+(.+)$", s)
        if not m:
            break
        title = m.group(1).lower().rstrip(".")
        if title not in _HONORIFICS:
            break
        s = m.group(2).strip()
    return s


def normalize_person_name(value) -> str:
    """Person list name: strip Mr/Mrs then proper capitalisation."""
    if value is None or value == "":
        return "—" if value is None else value
    s = strip_name_titles(value)
    if not s:
        return "—"
    return normalize_caps(s)


def normalize_caps(value) -> str:
    """
    Proper capitalisation for client / people names on lists and labels.

    Examples:
      ACME LIMITED → Acme Limited
      john smith → John Smith
      o'brien → O'Brien

    Preserves company numbers, IND- shells, and common acronyms (UK, LLP, HMRC…).
    Safe to call repeatedly (idempotent for already-normalised text).
    """
    if value is None or value == "":
        return "—" if value is None else value
    s = str(value).strip()
    if not s or s == "—":
        return s

    # Preserve pure company numbers / IND- shells
    if s.upper().startswith("IND-") or (s.replace(" ", "").isalnum() and s.isdigit()):
        return s

    small = {"of", "and", "the", "for", "in", "on", "at", "to", "a", "an", "&"}
    force = {
        "ltd": "Ltd",
        "limited": "Limited",
        "llp": "LLP",
        "plc": "PLC",
        "llc": "LLC",
        "uk": "UK",
        "gb": "GB",
        "eu": "EU",
        "usa": "USA",
        "hmrc": "HMRC",
        "vat": "VAT",
        "sa": "SA",
        "t/a": "t/a",
        "ta": "t/a",
    }

    parts = s.replace("/", " / ").split()
    out = []
    for i, raw in enumerate(parts):
        if raw in ("/", "-", "&"):
            out.append(raw)
            continue
        if raw.startswith("(") and raw.endswith(")") and len(raw) > 2:
            inner = raw[1:-1]
            ilow = inner.lower()
            if ilow in force:
                out.append(f"({force[ilow]})")
            elif len(inner) <= 3:
                out.append(f"({inner.upper()})")
            else:
                out.append(f"({inner[:1].upper() + inner[1:].lower()})")
            continue

        core = raw
        trail = ""
        while core and core[-1] in ".,;:)":
            trail = core[-1] + trail
            core = core[:-1]
        lead = ""
        while core and core[0] in "('\"“":
            lead += core[0]
            core = core[1:]

        low = core.lower()
        if low in force:
            word = force[low]
        elif low in small and i > 0:
            word = low
        elif "-" in core:
            word = "-".join(
                (p[:1].upper() + p[1:].lower()) if p else ""
                for p in core.split("-")
            )
        elif "'" in core or "’" in core:
            sep = "'" if "'" in core else "’"
            bits = core.split(sep)
            word = sep.join(
                (b[:1].upper() + b[1:].lower()) if b else "" for b in bits
            )
        else:
            word = core[:1].upper() + core[1:].lower() if core else ""
        out.append(lead + word + trail)

    text = " ".join(out)
    text = text.replace(" / ", " / ")
    return text
