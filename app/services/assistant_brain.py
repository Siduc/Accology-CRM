"""
Accologise AI brain: NLU + read tools + plan assembly.

Writes never happen here — only signed PendingPlan for user confirm.
Uses SpaceXAI (xAI) when XAI_API_KEY is set; strong heuristics otherwise.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import AI_ASSISTANT_ENABLED, AI_ASSISTANT_HEURISTIC, AI_MODEL, XAI_API_KEY
from app.models.client import Client
from app.models.job import Job
from app.models.prospecting import Prospect
from app.services.assistant_actions import (
    _normalise_officer_list,
    build_onboard_plan,
)
from app.services.assistant_plans import (
    PendingPlan,
    PlanStep,
    extract_company_number,
    extract_quoted_or_name,
    resolve_relative_date,
    sign_plan,
)
from app.services.company_numbers import normalize_company_number
from app.services.companies_house import (
    fetch_company_officers,
    fetch_company_profile,
    search_companies,
    summarize_profile_dates,
)
from app.services.prospecting import list_prospects

logger = logging.getLogger("accountant_crm.assistant_brain")

SYSTEM_PROMPT = """You are **Si**, Accologise practice assistant. You operate the CRM the same way a staff user would.

## Golden rules
1. **CRM first** — search clients/people/jobs already in Accologise before Companies House.
2. **Confirm before write** — any create/edit/delete returns a plan; wait for Yes.
3. **Never invent** company numbers, fees, officers, or balances.
4. **Navigate freely** — open any screen (dashboard, WIP, jobs, people, sales, bank…).
5. After creating something, **open it** (navigate to the new job/client/task).

## Entities
- **Companies** (`/clients`) = limited companies / firms (not IND- shells).
- **People** (`/people`) = contacts and individual tax clients.
- **Individual shells** = Client with type Individual / company_number IND-###### for SA/tax-only people.
- **Jobs** = work units (Accounts, Self Assessment, CS, VAT, Payroll…).
- **Tasks** = follow-ups on the task ledger (not the same as jobs).
- **Prospects** = pipeline before client conversion.

## Job type logic (must follow)
| Type | Who owns it | Period end default | Due date logic |
|------|-------------|--------------------|----------------|
| **Self Assessment (SAR)** | A **person** (individual shell), NEVER a Ltd company | 5 April (latest tax year end) | 31 January following that tax year |
| **Accounts** | Company client | Prior 31 Dec if unknown | PE + ~9 months (practice calculate_dates) |
| **Corporation Tax** | Company | same family as Accounts | CT rules in calculate_dates |
| **Confirmation Statement** | Company | from CH / user | PE + 14 days |
| **Other** | Client | as given | generic |

SAR for “director of Acme Ltd” → find Acme in CRM → pick director person → ensure individual shell → create SAR **on the person** with SAR dates.

## Status / WIP
- Open jobs use statuses like Planned, In Progress, Today, Tomorrow, This week, Review…
- Completed / Cancelled are closed; type lists (Accounts/SAR tiles) show **open** only.
- WIP ages jobs by due / focus bands; retainers sit under **Other**, not Accounts.

## Names
- Display strips Mr/Mrs/Miss/Ms for sort.
- Ltd ≈ Limited when matching company names.

## What you can do (map intent → action)
- go to / open / show → navigate
- find / look up [name] → CRM search (not CH unless asked)
- open client / company → /clients/{id}
- open job #n → /jobs/{id}
- create accounts/sar/cs job → create_job (+ dates)
- fill/recalculate dates → fill_job_dates (type-aware)
- set job status → update_job status
- create task / follow up → create_task
- add note → add_client_note
- create person / contact → create_person
- pull companies house / onboard → CH path only when asked

## Writing JSON plans
{"kind":"plan_request","intent":"navigate|open_client|open_job|create_job|update_job|fill_job_dates|create_task|add_note|create_person|onboard","payload":{...}}
For navigate: {"intent":"navigate","payload":{"href":"/working-capital/wip","label":"WIP"}}
"""


def assistant_status() -> dict:
    return {
        "enabled": bool(AI_ASSISTANT_ENABLED or AI_ASSISTANT_HEURISTIC),
        "llm": bool(AI_ASSISTANT_ENABLED and XAI_API_KEY),
        "heuristic": bool(AI_ASSISTANT_HEURISTIC),
        "model": AI_MODEL if XAI_API_KEY else None,
        "has_xai_key": bool(XAI_API_KEY),
    }


def handle_chat(
    db: Session,
    message: str,
    history: Optional[List[dict]] = None,
    page_context: Optional[dict] = None,
) -> dict:
    """
    Main entry. Returns:
      { kind, reply, plan?: {token, summary, steps, preview}, links?: [] }

    CRM-first: find/open clients, create jobs/tasks against the book.
    Companies House only when explicitly requested or true onboarding.
    """
    text = (message or "").strip()
    if not text:
        return {
            "kind": "clarify",
            "reply": (
                "Say or type what you need. Examples:\n"
                "• Find Acme Ltd (CRM)\n"
                "• Open client for Acme\n"
                "• Create SAR for director of Acme\n"
                "• Create Accounts job for Acme\n"
                "• Pull Companies House for 12345678 (CH only when you ask)"
            ),
        }

    low = text.lower()
    wants_ch = _wants_companies_house(low)

    # 0) Navigate CRM screens by voice/type (dashboard, WIP, jobs, people…)
    nav = _try_navigate(db, text)
    if nav:
        return nav

    # 0b) Fill / recalculate dates on an existing job (Si can edit)
    fill = _try_fill_job_dates(db, text)
    if fill:
        return fill

    status_plan = _try_set_job_status(db, text)
    if status_plan:
        return status_plan

    # 1) CRM job / task / open / find  (before CH)
    job_plan = _try_job_heuristic(db, text)
    if job_plan:
        return job_plan

    task_only = _try_task_heuristic(db, text)
    if task_only:
        return task_only

    open_c = _try_open_client(db, text)
    if open_c:
        return open_c

    crm_lookup = _try_crm_lookup(db, text)
    if crm_lookup:
        return crm_lookup

    query = _try_read_query(db, text)
    if query:
        return query

    # 2) CH / onboard only when user asks for CH or classic onboard with CN
    if wants_ch:
        onboard = _try_onboard_heuristic(db, text)
        if onboard:
            return onboard
        lookup = _try_ch_lookup(db, text)
        if lookup:
            return lookup
    else:
        # Onboard only if clearly "new prospect + company number + create"
        # and not a job/tax request for an existing book client
        if (
            extract_company_number(text)
            and any(w in low for w in ("prospect", "onboard", "new client"))
            and any(w in low for w in ("create", "add", "set up", "setup"))
            and "job" not in low
            and "sar" not in low
            and "self assessment" not in low
        ):
            onboard = _try_onboard_heuristic(db, text)
            if onboard:
                return onboard

    # 3) LLM if configured (tools include CRM + CH)
    if AI_ASSISTANT_ENABLED and XAI_API_KEY:
        try:
            return _llm_chat(db, text, history or [], page_context or {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("LLM chat failed")
            return {
                "kind": "message",
                "reply": (
                    f"AI model error: {exc}. "
                    "Try: Find [company], Open client [name], "
                    "Create SAR for director of [company], Create Accounts job for [company]."
                ),
            }

    return {
        "kind": "clarify",
        "reply": (
            "I work from the **CRM book** first. Try:\n"
            "• Find / open **[company name]**\n"
            "• Create **Accounts** / **SAR** / **CS** job for **[company]**\n"
            "• Create SAR for **director** of **[company]**\n"
            "• Show overdue jobs\n"
            "• Only when needed: pull **Companies House** for number 12345678"
        ),
    }


def _wants_companies_house(low: str) -> bool:
    return any(
        w in low
        for w in (
            "companies house",
            "company house",
            "from ch",
            "pull ch",
            "pull the record",
            "pull records",
            "from companies house",
            "ch search",
            "ch lookup",
            "onboard",
        )
    )


def _plan_response(plan: PendingPlan, reply: str) -> dict:
    token = sign_plan(plan)
    return {
        "kind": "plan",
        "reply": reply,
        "plan": {
            "token": token,
            "summary": plan.summary,
            "steps": [
                {"op": s.op, "label": s.label, "detail": s.detail}
                for s in plan.steps
            ],
            "preview": plan.preview,
        },
        "links": [],
    }


def _try_onboard_heuristic(db: Session, text: str) -> Optional[dict]:
    low = text.lower()
    create_ish = any(
        w in low
        for w in (
            "create",
            "add",
            "set up",
            "setup",
            "new prospect",
            "new client",
            "onboard",
        )
    )
    prospect_ish = "prospect" in low or "client" in low or "company" in low
    ch_ish = any(
        w in low
        for w in (
            "companies house",
            "company house",
            "pull the record",
            "pull records",
            "from ch",
            "officers",
            "directors",
            "company number",
        )
    )
    cn = extract_company_number(text)
    if not (create_ish and (prospect_ish or ch_ish) and cn):
        # Still allow if they give number + directors/task without "create"
        if not (cn and ch_ish and ("director" in low or "task" in low or "follow" in low)):
            if not (create_ish and cn):
                return None

    want_contacts = any(
        w in low for w in ("director", "officer", "contact", "people")
    ) or "companies house" in low or "pull" in low
    want_task = any(w in low for w in ("task", "follow-up", "follow up", "reminder"))
    # Default: if onboard-style, do full package
    if create_ish and cn and ("prospect" in low or "client" in low):
        want_contacts = want_contacts or True
        if "task" in low or "friday" in low or "follow" in low:
            want_task = True
        # Primary demo sentence always wants full package
        if "companies house" in low or "pull" in low:
            want_contacts = True

    # If they said the full classic sentence, force full package
    if "companies house" in low and ("director" in low or "contact" in low):
        want_contacts = True
    if "task" in low or "friday" in low:
        want_task = True

    # Fetch CH — never invent
    prof = fetch_company_profile(cn)
    if not prof.ok:
        return {
            "kind": "clarify",
            "reply": (
                f"Could not load Companies House for {cn}: {prof.error or 'unknown error'}. "
                "Check the company number and that COMPANIES_HOUSE_API_KEY is set."
            ),
        }

    data = prof.profile or {}
    summary = summarize_profile_dates(data)
    name = (
        extract_quoted_or_name(text)
        or summary.get("company_name")
        or data.get("company_name")
        or cn
    )
    addr = data.get("registered_office_address") or {}
    address = {
        "address_line1": addr.get("address_line_1"),
        "address_line2": addr.get("address_line_2"),
        "town": addr.get("locality"),
        "postcode": addr.get("postal_code"),
    }

    officers: List[dict] = []
    if want_contacts:
        off = fetch_company_officers(cn)
        if off.ok:
            officers = _normalise_officer_list(off.profile)
            # Active only
            officers = [o for o in officers if not o.get("resigned_on")]
        else:
            return {
                "kind": "clarify",
                "reply": (
                    f"Company {name} ({cn}) found, but officers could not be loaded: "
                    f"{off.error or 'error'}. Retry or continue without contacts."
                ),
            }

    task_due = None
    task_title = f"Follow up — {name}"
    if want_task:
        # Extract relative date phrase
        m = re.search(
            r"(?:task|follow[\s-]?up|reminder|due)\s+(?:for\s+)?(.+?)(?:\.|$)",
            text,
            re.I,
        )
        due_phrase = None
        if m:
            due_phrase = m.group(1).strip()
        for phrase in (
            "next friday",
            "next monday",
            "next tuesday",
            "next wednesday",
            "next thursday",
            "tomorrow",
            "this friday",
            "friday",
        ):
            if phrase in low:
                due_phrase = phrase
                break
        task_due = resolve_relative_date(due_phrase or "next friday")

    plan = build_onboard_plan(
        company_name=name,
        company_number=cn,
        officers=officers,
        task_title=task_title,
        task_due=task_due,
        want_contacts=want_contacts,
        want_task=want_task,
        want_prospect=True,
        want_client=want_contacts or want_task or True,
        pull_ch=True,
        address=address,
    )

    officer_lines = ""
    if officers:
        officer_lines = "\nDirectors/officers:\n" + "\n".join(
            f"  • {o.get('name')} — {o.get('role')}" for o in officers[:12]
        )
        if len(officers) > 12:
            officer_lines += f"\n  … +{len(officers) - 12} more"

    due_txt = task_due.strftime("%d %b %Y") if task_due else "—"
    reply = (
        f"Ready to set this up from Companies House:\n\n"
        f"**{name}** ({cn})\n"
        f"Status: {summary.get('company_status') or data.get('company_status') or '—'}"
        f"{officer_lines}\n\n"
        f"Follow-up task: {task_title if want_task else '—'} · due {due_txt if want_task else '—'}\n\n"
        f"I’ll create a Prospect and Client (so contacts and the task can attach). "
        f"Confirm below — nothing is saved until you say Yes."
    )
    return _plan_response(plan, reply)


def _norm_company_search_key(s: str) -> str:
    """Collapse Ltd/Limited/etc so spoken names match the book."""
    t = (s or "").lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)
    # Spoken form → short form
    for a, b in (
        (r"\blimited\b", "ltd"),
        (r"\bltd\b", "ltd"),
        (r"\bpublic limited company\b", "plc"),
        (r"\bp\.?l\.?c\.?\b", "plc"),
        (r"\bllp\b", "llp"),
        (r"\bthe\b", " "),
        (r"\bcompany\b", " "),
        (r"\bco\b", " "),
    ):
        t = re.sub(a, b, t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _search_crm_clients(db: Session, q: str, *, limit: int = 12) -> List[Client]:
    """
    Name / number search against Accologise clients.

    Tolerant matching: 'We Fit Bathrooms Limited' ≈ 'WE FIT BATHROOMS LTD'.
    """
    needle = (q or "").strip()
    if not needle:
        return []
    cn = normalize_company_number(needle) if re.search(r"\d", needle) else None
    if cn and len(cn) >= 6:
        hit = db.query(Client).filter(Client.company_number == cn).first()
        if hit:
            return [hit]

    # Broad SQL pre-filter on significant tokens (ignore ltd/limited noise)
    key = _norm_company_search_key(needle)
    tokens = [
        t
        for t in key.split()
        if t not in ("ltd", "plc", "llp", "and", "of", "the") and len(t) >= 2
    ]
    if not tokens:
        tokens = [t for t in key.split() if len(t) >= 2]

    query = db.query(Client)
    if tokens:
        # Require first substantial token in name (keeps SQL cheap)
        query = query.filter(Client.company_name.ilike(f"%{tokens[0]}%"))
    else:
        like = f"%{needle}%"
        query = query.filter(
            (Client.company_name.ilike(like))
            | (Client.company_number.ilike(like))
            | (Client.contact_name.ilike(like))
        )
    candidates = query.order_by(Client.company_name).limit(80).all()

    scored: List[tuple] = []
    needle_key = key
    for c in candidates:
        name_key = _norm_company_search_key(c.company_name or "")
        if not name_key:
            continue
        score = 0
        if name_key == needle_key:
            score = 100
        elif needle_key in name_key or name_key in needle_key:
            score = 80
        else:
            # all tokens present (order-independent)
            if tokens and all(tok in name_key for tok in tokens):
                score = 60 + min(20, len(tokens) * 3)
            else:
                # majority of tokens
                hit_n = sum(1 for tok in tokens if tok in name_key)
                if tokens and hit_n >= max(1, (len(tokens) + 1) // 2):
                    score = 40 + hit_n * 5
        if score > 0:
            scored.append((score, c))

    if not scored:
        # Fallback: plain ilike full string
        like = f"%{needle}%"
        return (
            db.query(Client)
            .filter(
                (Client.company_name.ilike(like))
                | (Client.company_number.ilike(like))
                | (Client.contact_name.ilike(like))
            )
            .order_by(Client.company_name)
            .limit(limit)
            .all()
        )

    scored.sort(key=lambda x: (-x[0], (x[1].company_name or "").lower()))
    # Prefer exact-ish; if top score is strong, return only top cluster
    best = scored[0][0]
    out = [c for s, c in scored if s >= best - 15][:limit]
    return out


def _client_links(c: Client) -> List[dict]:
    return [
        {"label": "Open client", "href": f"/clients/{c.id}"},
        {"label": "Jobs", "href": f"/jobs?client_id={c.id}"},
        {"label": "New job", "href": f"/jobs/new?client_id={c.id}"},
    ]


def _try_crm_lookup(db: Session, text: str) -> Optional[dict]:
    """Find a company/person already in the CRM (not Companies House)."""
    low = text.lower()
    # Don't steal pure job-create lines (handled elsewhere)
    if any(w in low for w in ("create job", "create accounts", "create sar", "create cs")):
        return None
    if not any(
        w in low
        for w in (
            "find",
            "look up",
            "lookup",
            "search",
            "show me",
            "where's",
            "where is",
            "who is",
            "client for",
            "company",
        )
    ):
        return None
    if _wants_companies_house(low):
        return None  # CH path owns explicit CH requests

    q = text
    m = re.search(
        r"(?:find|look\s*up|lookup|search|show\s*me|open)\s+"
        r"(?:the\s+)?(?:client|company|companies|crm)?\s*(?:for\s+|named\s+|called\s+)?(.+)",
        text,
        re.I,
    )
    if m:
        q = m.group(1).strip().strip("\"'")
    q = re.sub(
        r"\b(in\s+the\s+crm|in\s+accologise|please|thanks)\b",
        "",
        q,
        flags=re.I,
    ).strip(" .,")
    # Strip trailing intent words
    q = re.sub(
        r"\b(and\s+create.*|create.*|set\s+up.*)$",
        "",
        q,
        flags=re.I,
    ).strip(" .,")
    if len(q) < 2:
        return None

    clients = _search_crm_clients(db, q)
    if not clients:
        # Fall through so LLM or CH (if asked) can help
        return {
            "kind": "message",
            "reply": (
                f"No **CRM** client matched “{q}”. "
                "Check the spelling, or say **pull Companies House for …** if this is a new company."
            ),
            "links": [{"label": "All companies", "href": "/clients"}],
        }

    if len(clients) == 1:
        c = clients[0]
        # People linked
        people_bits = []
        try:
            for p in (c.people or [])[:8]:
                people_bits.append(
                    f"  • {p.display_name()}"
                    + (f" — {p.role}" if p.role else "")
                )
        except Exception:
            pass
        open_jobs = (
            db.query(Job)
            .filter(
                Job.client_id == c.id,
                Job.status.in_(Job.OPEN_STATUSES),
            )
            .limit(8)
            .all()
        )
        lines = [
            f"**{c.display_name()}** (in CRM)",
            f"Number: {c.company_number or '—'} · Status: {c.overall_status or '—'}",
        ]
        if people_bits:
            lines.append("People / directors:")
            lines.extend(people_bits)
        if open_jobs:
            lines.append("Open jobs:")
            for j in open_jobs:
                lines.append(f"  • {j.type or j.title} · {j.status or '—'} (#{j.id})")
        else:
            lines.append("Open jobs: none")
        lines.append("\nSay e.g. **create SAR for director of this company** or **create Accounts job**.")
        return {
            "kind": "message",
            "reply": "\n".join(lines),
            "links": _client_links(c),
        }

    lines = [f"**{len(clients)} CRM clients** matching “{q}”:"]
    links = []
    for c in clients[:10]:
        lines.append(
            f"  • {c.display_name()} — {c.company_number or 'no number'} ({c.overall_status or '—'})"
        )
        links.append(
            {"label": c.display_name()[:36], "href": f"/clients/{c.id}"}
        )
    lines.append("\nName one exactly, or open a link.")
    return {"kind": "message", "reply": "\n".join(lines), "links": links}


def _try_open_client(db: Session, text: str) -> Optional[dict]:
    low = text.lower()
    if not any(
        w in low
        for w in (
            "open client",
            "show client",
            "client screen",
            "client page",
            "go to client",
            "open company",
            "show company",
        )
    ):
        return None
    m = re.search(
        r"(?:open|show|go to)\s+(?:the\s+)?(?:client|company)\s+"
        r"(?:screen\s+|page\s+)?(?:for\s+)?(.+)",
        text,
        re.I,
    )
    q = (m.group(1) if m else "").strip().strip("\"'")
    if len(q) < 2:
        q = re.sub(r"^(open|show|go to)\s+(client|company)\s*", "", text, flags=re.I).strip()
    clients = _search_crm_clients(db, q, limit=5)
    if not clients:
        return {
            "kind": "message",
            "reply": f"No CRM client matched “{q}”.",
            "links": [{"label": "Companies list", "href": "/clients"}],
        }
    if len(clients) > 1:
        return {
            "kind": "message",
            "reply": "Several matches — pick one:\n"
            + "\n".join(f"  • {c.display_name()}" for c in clients),
            "links": [
                {"label": c.display_name()[:36], "href": f"/clients/{c.id}"}
                for c in clients
            ],
        }
    c = clients[0]
    return {
        "kind": "message",
        "reply": f"Opening **{c.display_name()}** in the CRM.",
        "links": _client_links(c),
        "navigate": f"/clients/{c.id}",
    }


def _extract_company_from_utterance(text: str) -> str:
    """Pull company name from natural phrases (voice-friendly)."""
    quoted = extract_quoted_or_name(text)
    if quoted:
        return quoted.strip()
    # "Person Name of COMPANY" (when user confirms a named director)
    m = re.search(
        r"\bof\s+((?:(?!limited|ltd|llp|plc)[A-Za-z0-9&'.\-]+\s+){0,6}(?:Limited|Ltd|LLP|PLC|[A-Za-z0-9&'.\-]+))\s*$",
        text.strip(),
        re.I,
    )
    if m and re.search(r"\b(limited|ltd|llp|plc|company)\b", m.group(1), re.I):
        return m.group(1).strip().strip("\"'.,")
    # director of COMPANY
    m = re.search(
        r"director(?:s)?\s+of\s+(.+?)(?:\s+please|\s+and\s+create|\s*$)",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip().strip("\"'.,")
    # SAR / job for COMPANY
    m = re.search(
        r"(?:sar|self[\s-]?assessment|accounts|cs|confirmation statement|job|tax)\s+"
        r"(?:job\s+)?(?:for|on|at)\s+(?:the\s+)?(?:director(?:s)?\s+of\s+)?(.+?)(?:\s+please|\s*$)",
        text,
        re.I,
    )
    if m:
        q = m.group(1).strip().strip("\"'.,")
        # Don't treat a person name as the company
        if re.search(r"\b(limited|ltd|llp|plc)\b", q, re.I) or len(q.split()) >= 2:
            # If "Benjamin of We Fit..." take company after of
            m2 = re.search(r"\bof\s+(.+)$", q, re.I)
            if m2 and re.search(r"\b(limited|ltd|llp|plc)\b", m2.group(1), re.I):
                return m2.group(1).strip()
            return q
    m = re.search(
        r"(?:for|of|on)\s+(?:the\s+)?(?:director(?:s)?\s+of\s+)?(.+?)(?:\s+please|\s+and\s+|$)",
        text,
        re.I,
    )
    if m:
        q = m.group(1).strip().strip("\"'")
        q = re.sub(
            r"\b(director|directors|his|her|their|its|a|an|the|job)\b",
            " ",
            q,
            flags=re.I,
        )
        q = re.sub(r"\s+", " ", q).strip(" .,")
        m2 = re.search(r"\bof\s+(.+)$", q, re.I)
        if m2 and re.search(r"\b(limited|ltd|llp|plc)\b", m2.group(1), re.I):
            return m2.group(1).strip()
        return q
    return ""


def _try_fill_job_dates(db: Session, text: str) -> Optional[dict]:
    """
    'Fill the dates' / 'set SAR dates' / 'recalculate dates on that job'.
    Uses job-type logic (SAR ≠ Accounts/CT).
    """
    low = text.lower()
    if not any(
        w in low
        for w in (
            "fill date",
            "fill the date",
            "set date",
            "add date",
            "put date",
            "recalculate date",
            "calc date",
            "calculate date",
            "dates on the job",
            "dates for the job",
            "update date",
            "fix dates",
            "fill in the date",
            "fill in dates",
            "statutory due",
            "period end",
        )
    ):
        return None

    from app.services.dates import default_period_end, calculate_dates

    # Prefer latest open SAR if mentioned
    jtype = ""
    if "sar" in low or "self assessment" in low:
        jtype = "Self Assessment"
    elif "account" in low or " ct" in low or "corporation" in low:
        jtype = "Accounts"
    elif "confirmation" in low or re.search(r"\bcs\b", low):
        jtype = "Confirmation Statement"

    job = None
    # job id in text
    m = re.search(r"\bjob\s*#?\s*(\d+)\b", text, re.I)
    if m:
        job = db.query(Job).filter(Job.id == int(m.group(1))).first()

    # company / person context
    company_q = _extract_company_from_utterance(text)
    clients = _search_crm_clients(db, company_q, limit=5) if company_q else []

    if not job and clients:
        q = db.query(Job).filter(Job.client_id == clients[0].id)
        if jtype:
            q = q.filter(Job.type == jtype)
        job = q.order_by(Job.id.desc()).first()

    if not job and jtype:
        # latest job of that type with empty statutory
        job = (
            db.query(Job)
            .filter(Job.type == jtype)
            .filter(
                (Job.statutory_due_date.is_(None)) | (Job.period_end.is_(None))
            )
            .order_by(Job.id.desc())
            .first()
        )

    if not job:
        # most recently created assistant/open job missing dates
        job = (
            db.query(Job)
            .filter(
                (Job.statutory_due_date.is_(None)) | (Job.period_end.is_(None))
            )
            .order_by(Job.id.desc())
            .first()
        )

    if not job:
        return {
            "kind": "clarify",
            "reply": (
                "Which job should I fill dates on? "
                "Say **fill dates on job #123** or **fill SAR dates for Benjamin Robinson**."
            ),
        }

    jt = job.type or jtype or "Self Assessment"
    pe = job.period_end or default_period_end(jt)
    statutory, ts, tc = calculate_dates(jt, pe)
    pe_s = pe.isoformat() if pe else "—"
    due_s = statutory.isoformat() if statutory else "—"

    plan = PendingPlan(
        summary=f"Fill {jt} dates on job #{job.id}",
        steps=[
            PlanStep(
                op="fill_job_dates",
                label=f"Update job #{job.id} dates ({jt})",
                detail=f"PE {pe_s} · statutory due {due_s}",
                params={
                    "job_id": job.id,
                    "type": jt,
                    "period_end": pe_s if pe else None,
                    "fill_dates": True,
                    "recalculate": True,
                },
            )
        ],
        preview={
            "job_id": job.id,
            "type": jt,
            "period_end": pe_s,
            "statutory_due": due_s,
        },
    )
    client_name = job.client.display_name() if job.client else "—"
    reply = (
        f"I’ll set **{jt}** dates on job **#{job.id}** for **{client_name}** "
        f"(not Accounts/CT logic):\n"
        f"• Period end: **{pe_s}**\n"
        f"• Statutory due: **{due_s}**\n"
        f"• Target start: **{ts.isoformat() if ts else '—'}**\n"
        f"• Target complete: **{tc.isoformat() if tc else '—'}**\n\n"
        "Say **Yes** to save."
    )
    return _plan_response(plan, reply)


def _try_navigate(db: Session, text: str) -> Optional[dict]:
    """Voice/type navigation around the whole CRM (screens + deep links)."""
    from app.services.assistant_crm_logic import match_screen

    low = text.lower().strip()
    # Ignore pure create/edit that isn't "open new job form"
    if any(
        w in low
        for w in (
            "create sar for",
            "create accounts for",
            "create a sar",
            "pull companies house",
            "fill date",
            "fill the date",
        )
    ):
        return None

    # Open job #123 / open client Acme
    m = re.search(r"\b(?:open|show|go to)\s+job\s*#?\s*(\d+)\b", low)
    if m:
        href = f"/jobs/{m.group(1)}"
        return {
            "kind": "message",
            "reply": f"Opening **job #{m.group(1)}**.",
            "links": [{"label": f"Job #{m.group(1)}", "href": href}],
            "navigate": href,
        }
    m = re.search(r"\b(?:open|show|go to)\s+(?:client|company)\s+#?\s*(\d+)\b", low)
    if m:
        href = f"/clients/{m.group(1)}"
        return {
            "kind": "message",
            "reply": f"Opening **client #{m.group(1)}**.",
            "links": [{"label": f"Client #{m.group(1)}", "href": href}],
            "navigate": href,
        }

    nav_verb = any(
        w in low
        for w in (
            "go to",
            "open the",
            "open my",
            "take me",
            "show me the",
            "show the",
            "navigate",
            "switch to",
            "take me to",
        )
    ) or low in {
        "dashboard",
        "home",
        "wip",
        "jobs",
        "people",
        "companies",
        "clients",
        "prospects",
        "tasks",
        "settings",
        "sales",
        "bank",
        "notes",
        "documents",
        "debtors",
        "creditors",
        "vat",
        "groups",
        "asana",
    }
    if not nav_verb and not low.startswith("go "):
        if not re.match(
            r"^(open|show)\s+(the\s+)?("
            r"dashboard|home|wip|jobs|people|companies|clients|prospects|tasks|"
            r"settings|sales|bank|notes|documents|debtors|creditors|vat|groups|"
            r"accounts jobs|sar|new job|add client|new client"
            r")\b",
            low,
        ):
            return None

    hit = match_screen(low)
    if not hit:
        return None
    return {
        "kind": "message",
        "reply": f"Opening **{hit['label']}**.",
        "links": [{"label": hit["label"], "href": hit["href"]}],
        "navigate": hit["href"],
    }


def _try_set_job_status(db: Session, text: str) -> Optional[dict]:
    """Mark job complete / set status via voice."""
    low = text.lower()
    if not any(
        w in low
        for w in (
            "mark complete",
            "mark completed",
            "set status",
            "change status",
            "mark as complete",
            "mark job",
            "complete job",
        )
    ):
        return None
    m = re.search(r"\bjob\s*#?\s*(\d+)\b", text, re.I)
    if not m:
        return {
            "kind": "clarify",
            "reply": "Which job? e.g. **mark job #1108 complete**.",
        }
    jid = int(m.group(1))
    job = db.query(Job).filter(Job.id == jid).first()
    if not job:
        return {"kind": "message", "reply": f"No job #{jid} found."}
    status = "Completed"
    if "hold" in low:
        status = "On hold"
    elif "progress" in low:
        status = "In Progress"
    elif "planned" in low:
        status = "Planned"
    elif "today" in low:
        status = "Today"
    elif "cancel" in low:
        status = "Cancelled"
    plan = PendingPlan(
        summary=f"Set job #{jid} → {status}",
        steps=[
            PlanStep(
                op="set_job_status",
                label=f"Job #{jid} status → {status}",
                detail=job.title or job.type or "",
                params={"job_id": jid, "status": status},
            )
        ],
        preview={"job_id": jid, "status": status},
    )
    return _plan_response(
        plan,
        f"Set **job #{jid}** ({job.client.display_name() if job.client else '—'} · "
        f"{job.type or 'job'}) to **{status}**?\nSay **Yes** to confirm.",
    )


def _try_job_heuristic(db: Session, text: str) -> Optional[dict]:
    """Create Accounts / CS / SAR job against CRM clients (and SAR for directors)."""
    low = text.lower()
    if not any(
        w in low
        for w in (
            "create job",
            "add job",
            "new job",
            "create accounts",
            "accounts job",
            "create sar",
            "create a sar",
            "sar job",
            "self assessment",
            "create cs",
            "confirmation statement job",
            "set up a tax",
            "setup a tax",
            "set up tax",
            "tax job",
        )
    ):
        # "create a SAR for director of X" / voice typos
        if not (
            ("sar" in low or "self assessment" in low or "self assessment" in low)
            and any(w in low for w in ("create", "add", "set up", "setup", "new"))
        ):
            return None

    # Job type
    jtype = "Accounts"
    if "sar" in low or "self assessment" in low or "self-assessment" in low:
        jtype = "Self Assessment"
    elif re.search(r"\bcs\b", low) or "confirmation statement" in low:
        jtype = "Confirmation Statement"

    # Company needle — robust for voice ("… director of We Fit Bathrooms Limited")
    company_q = _extract_company_from_utterance(text)

    # Named person for SAR: "create SAR for Benjamin Francis Robinson"
    person_q = ""
    if jtype == "Self Assessment":
        mp = re.search(
            r"(?:sar|self[\s-]?assessment)\s+(?:job\s+)?for\s+"
            r"(?!the\s+director)([A-Za-z][A-Za-z'.\-]+(?:\s+[A-Za-z][A-Za-z'.\-]+){0,4})"
            r"(?:\s+of\s+|\s+at\s+|\s+please|\s*$)",
            text,
            re.I,
        )
        if mp:
            person_q = mp.group(1).strip()
            # if capture still has company suffix, split
            if re.search(r"\b(limited|ltd|llp|plc)\b", person_q, re.I):
                person_q = ""

    cn = extract_company_number(text)
    clients: List[Client] = []
    if cn:
        clients = _search_crm_clients(db, cn, limit=3)
    if not clients and company_q and len(company_q) >= 2:
        clients = _search_crm_clients(db, company_q, limit=8)

    # Person-first SAR (no company needed if we can find the contact)
    if jtype == "Self Assessment" and person_q and len(person_q) >= 3:
        from app.models.person import Person

        people = (
            db.query(Person)
            .filter(Person.full_name.ilike(f"%{person_q}%"))
            .order_by(Person.id)
            .limit(8)
            .all()
        )
        if len(people) == 1 or (people and company_q):
            p0 = people[0]
            if company_q and len(people) > 1:
                # Prefer person linked to matched company
                for p in people:
                    cos = [c for c in (p.clients or []) if c in clients or (
                        company_q and any(
                            company_q.lower()[:8] in (c.company_name or "").lower()
                            for c in (p.clients or [])
                        )
                    )]
                    if cos or any(
                        _norm_company_search_key(company_q)
                        in _norm_company_search_key(c.company_name or "")
                        for c in (p.clients or [])
                    ):
                        p0 = p
                        break
            if people:
                if not company_q and len(people) > 1:
                    return {
                        "kind": "message",
                        "reply": "Several people match — which one?\n"
                        + "\n".join(f"  • {p.display_name()}" for p in people[:8]),
                        "links": [
                            {
                                "label": p.display_name()[:36],
                                "href": f"/people/{p.id}/edit",
                            }
                            for p in people[:8]
                        ],
                    }
                p0 = people[0] if len(people) == 1 else p0
                from app.services.dates import default_period_end, calculate_dates

                pe = default_period_end("Self Assessment")
                statutory, _, _ = calculate_dates("Self Assessment", pe)
                pe_s = pe.isoformat() if pe else None
                due_s = statutory.isoformat() if statutory else None
                plan = PendingPlan(
                    summary=f"SAR job for {p0.display_name()}",
                    steps=[
                        PlanStep(
                            op="create_job",
                            label="Create Self Assessment job",
                            detail=(
                                f"{p0.display_name()} · PE {pe_s or '—'} · due {due_s or '—'}"
                            ),
                            params={
                                "type": "Self Assessment",
                                "person_id": p0.id,
                                "title": f"Self Assessment — {p0.display_name()}",
                                "period_end": pe_s,
                            },
                        )
                    ],
                    preview={
                        "person_name": p0.display_name(),
                        "period_end": pe_s,
                        "statutory_due": due_s,
                    },
                )
                return _plan_response(
                    plan,
                    f"Create a **Self Assessment** job for **{p0.display_name()}** "
                    f"(person — not a limited company)?\n"
                    f"Dates use **SAR** rules: PE **{pe_s or '—'}**, due **{due_s or '—'}**.\n"
                    "Say **Yes** to confirm, or tap Yes — do it.\n"
                    "Nothing is saved until you confirm.",
                )

    if not clients:
        return {
            "kind": "clarify",
            "reply": (
                f"I couldn’t match a **CRM company** from “{company_q or 'that name'}”. "
                f"Try the name as on the Companies list (Ltd/Limited both work), "
                f"e.g. **create SAR for director of We Fit Bathrooms Ltd**."
            ),
            "links": [{"label": "Companies", "href": "/clients"}],
        }
    if len(clients) > 1:
        return {
            "kind": "message",
            "reply": "Several CRM companies match — which one?\n"
            + "\n".join(
                f"  • {c.display_name()} ({c.company_number or '—'})" for c in clients[:8]
            ),
            "links": [
                {"label": c.display_name()[:36], "href": f"/clients/{c.id}"}
                for c in clients[:8]
            ],
        }

    company = clients[0]

    # SAR for director(s) of a company
    if jtype == "Self Assessment" and any(
        w in low for w in ("director", "officer", "contact")
    ):
        from app.models.person import Person

        people = list(company.people or [])
        # Prefer director-like roles
        directors = [
            p
            for p in people
            if "director" in (p.role or "").lower()
            or not (p.role or "").strip()
        ]
        if not directors:
            directors = people
        if not directors:
            return {
                "kind": "clarify",
                "reply": (
                    f"**{company.display_name()}** is in the CRM but has no people/directors linked. "
                    "Add contacts on the client screen, or name the person for the SAR."
                ),
                "links": _client_links(company),
            }

        # Optional named director in utterance
        named = None
        nm = re.search(
            r"(?:director|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            text,
        )
        if nm and "of" not in nm.group(1).lower():
            needle = nm.group(1).strip()
            for p in directors:
                if needle.lower() in (p.full_name or "").lower():
                    named = p
                    break
        targets = [named] if named else directors[:1]  # one clear SAR unless named
        if not named and len(directors) > 1:
            # Plan for first but list options
            lines = [
                f"**{company.display_name()}** directors in CRM:",
            ]
            for p in directors[:10]:
                lines.append(f"  • {p.display_name()}" + (f" — {p.role}" if p.role else ""))
            lines.append(
                "\nSay **create SAR for [name]** to pick one, or confirm below for the first."
            )
            p0 = directors[0]
            plan = PendingPlan(
                summary=f"SAR job for {p0.display_name()} (director of {company.display_name()})",
                steps=[
                    PlanStep(
                        op="create_job",
                        label="Create Self Assessment job",
                        detail=f"{p0.display_name()} · linked to {company.display_name()}",
                        params={
                            "type": "Self Assessment",
                            "person_id": p0.id,
                            "title": f"Self Assessment — {p0.display_name()}",
                            "notes": f"Director of {company.display_name()} (CRM)",
                        },
                    )
                ],
                preview={
                    "company_name": company.display_name(),
                    "person_name": p0.display_name(),
                },
            )
            return _plan_response(plan, "\n".join(lines) + "\n\nNothing saved until you confirm.")

        p0 = targets[0]
        from app.services.dates import default_period_end, calculate_dates

        pe = default_period_end("Self Assessment")
        statutory, _, _ = calculate_dates("Self Assessment", pe)
        pe_s = pe.isoformat() if pe else None
        due_s = statutory.isoformat() if statutory else None
        plan = PendingPlan(
            summary=f"SAR job for {p0.display_name()} (director of {company.display_name()})",
            steps=[
                PlanStep(
                    op="create_job",
                    label="Create Self Assessment job",
                    detail=(
                        f"{p0.display_name()} · PE {pe_s or '—'} · due {due_s or '—'}"
                    ),
                    params={
                        "type": "Self Assessment",
                        "person_id": p0.id,
                        "title": f"Self Assessment — {p0.display_name()}",
                        "notes": f"Director of {company.display_name()} (CRM)",
                        "period_end": pe_s,
                    },
                )
            ],
            preview={
                "company_name": company.display_name(),
                "person_name": p0.display_name(),
                "period_end": pe_s,
                "statutory_due": due_s,
            },
        )
        reply = (
            f"Create a **Self Assessment** job for **{p0.display_name()}** "
            f"(director/contact of **{company.display_name()}** in the CRM)?\n"
            "Uses their individual client record — a company cannot have a SAR.\n"
            f"**SAR dates** (not Accounts/CT): PE **{pe_s or '—'}**, due **{due_s or '—'}**.\n"
            "Say **Yes** to confirm (or tap Yes — do it).\n"
            "Nothing is saved until you confirm."
        )
        return _plan_response(plan, reply)

    # Accounts / CS / SAR (without director) on the company client itself
    # For SAR without director on a Ltd company, warn and prefer people
    if jtype == "Self Assessment":
        from app.services.individuals import is_individual_shell

        if company and not is_individual_shell(company):
            people = list(company.people or [])
            if people:
                return {
                    "kind": "clarify",
                    "reply": (
                        f"**{company.display_name()}** is a company. "
                        "SAR is for a **person**. Linked people:\n"
                        + "\n".join(f"  • {p.display_name()}" for p in people[:10])
                        + "\n\nSay **create SAR for director of "
                        + f"{company.display_name()}** or name the person."
                    ),
                    "links": _client_links(company),
                }

    from app.services.dates import default_period_end, calculate_dates

    pe = default_period_end(jtype)
    statutory, _, _ = calculate_dates(jtype, pe) if pe else (None, None, None)
    pe_s = pe.isoformat() if pe else None
    due_s = statutory.isoformat() if statutory else None
    plan = PendingPlan(
        summary=f"{jtype} job for {company.display_name()}",
        steps=[
            PlanStep(
                op="create_job",
                label=f"Create {jtype} job",
                detail=(
                    f"{company.display_name()}"
                    + (f" · PE {pe_s}" if pe_s else "")
                    + (f" · due {due_s}" if due_s else "")
                ),
                params={
                    "type": jtype,
                    "client_id": company.id,
                    "title": f"{jtype} — {company.display_name()}",
                    "period_end": pe_s,
                },
            )
        ],
        preview={
            "company_name": company.display_name(),
            "company_number": company.company_number,
            "period_end": pe_s,
            "statutory_due": due_s,
        },
    )
    date_line = ""
    if pe_s or due_s:
        date_line = f"\nDates ({jtype}): PE **{pe_s or '—'}**, due **{due_s or '—'}**."
    reply = (
        f"Create **{jtype}** job for **{company.display_name()}** "
        f"({company.company_number or 'no number'}) in the CRM?"
        f"{date_line}\n"
        "Say **Yes** to confirm. Nothing is saved until you confirm."
    )
    return _plan_response(plan, reply)


def _try_ch_lookup(db: Session, text: str) -> Optional[dict]:
    """Companies House lookup — only when user explicitly wants CH."""
    low = text.lower()
    if not _wants_companies_house(low) and "companies house" not in low:
        # Require explicit CH for this path
        if not any(w in low for w in ("from companies house", "on companies house", "ch:")):
            return None

    cn = extract_company_number(text)
    if cn:
        prof = fetch_company_profile(cn)
        if not prof.ok:
            return {"kind": "message", "reply": f"CH lookup failed for {cn}: {prof.error}"}
        data = prof.profile or {}
        summary = summarize_profile_dates(data)
        off = fetch_company_officers(cn)
        officers = _normalise_officer_list(off.profile) if off.ok else []
        officers = [o for o in officers if not o.get("resigned_on")]
        lines = [
            f"**{summary.get('company_name') or data.get('company_name') or cn}** (Companies House)",
            f"Number: {cn}",
            f"Status: {summary.get('company_status') or data.get('company_status') or '—'}",
            f"Accounts due: {summary.get('accounts_due') or '—'}",
        ]
        if officers:
            lines.append("Officers:")
            for o in officers[:10]:
                lines.append(f"  • {o.get('name')} — {o.get('role')}")
        # Also note if already in CRM
        crm = _search_crm_clients(db, cn, limit=1)
        links = []
        if crm:
            lines.append(f"\nAlso in CRM as **{crm[0].display_name()}**.")
            links = _client_links(crm[0])
        return {
            "kind": "message",
            "reply": "\n".join(lines),
            "links": links,
        }

    m = re.search(r"(?:look\s*up|search|find)\s+(?:company\s+)?(.+)", text, re.I)
    q = (m.group(1) if m else text).strip()
    q = re.sub(r"\b(on|from|at)\s+companies\s+house\b", "", q, flags=re.I).strip(" .")
    if len(q) < 2:
        return {"kind": "clarify", "reply": "Which company name or number for Companies House?"}
    res = search_companies(q, items_per_page=8)
    if not res.ok:
        return {"kind": "message", "reply": f"CH search failed: {res.error}"}
    items = (res.profile or {}).get("items") or []
    if not items:
        return {"kind": "message", "reply": f"No Companies House matches for “{q}”."}
    lines = [f"Companies House matches for “{q}”:"]
    for it in items[:8]:
        lines.append(
            f"  • {it.get('title') or it.get('company_name')} — "
            f"{it.get('company_number')} ({it.get('company_status') or '—'})"
        )
    lines.append(
        "\nTo add to CRM: **Create prospect … company number … pull Companies House**."
    )
    return {"kind": "message", "reply": "\n".join(lines)}


def _try_read_query(db: Session, text: str) -> Optional[dict]:
    low = text.lower()
    if any(w in low for w in ("overdue job", "jobs overdue", "show overdue", "overdue work")):
        today = date.today()
        jobs = (
            db.query(Job)
            .filter(Job.status.in_(Job.OPEN_STATUSES))
            .order_by(Job.statutory_due_date.asc())
            .limit(80)
            .all()
        )
        overdue = []
        for j in jobs:
            due = j.statutory_due_date or j.target_completion
            if due and due < today:
                overdue.append(j)
        if not overdue:
            return {"kind": "message", "reply": "No overdue open jobs right now."}
        lines = [f"**{len(overdue)} overdue open job(s):**"]
        links = []
        for j in overdue[:15]:
            cname = j.client.display_name() if j.client else "—"
            due = j.statutory_due_date or j.target_completion
            lines.append(
                f"  • {cname} — {j.type or j.title} · due {due.isoformat() if due else '—'}"
            )
            links.append({"label": f"Job #{j.id}", "href": f"/jobs/{j.id}"})
        return {"kind": "message", "reply": "\n".join(lines), "links": links[:10]}

    if any(w in low for w in ("recent prospect", "new prospect", "latest prospect", "show prospect")):
        rows = list_prospects(db, open_only=False, limit=12)
        rows = sorted(rows, key=lambda p: p.created_at or date.min, reverse=True)[:10]
        if not rows:
            return {"kind": "message", "reply": "No prospects yet."}
        lines = ["**Recent prospects:**"]
        links = []
        for p in rows:
            lines.append(
                f"  • {p.display_name()} · {p.pipeline_status or '—'} · {p.company_number or 'no CN'}"
            )
            links.append(
                {
                    "label": p.display_name()[:40],
                    "href": f"/prospecting/prospects/{p.id}",
                }
            )
        return {"kind": "message", "reply": "\n".join(lines), "links": links}
    return None


def _try_task_heuristic(db: Session, text: str) -> Optional[dict]:
    low = text.lower()
    if not any(w in low for w in ("create task", "add task", "follow-up", "follow up task", "new task")):
        return None
    # Need a client reference
    cn = extract_company_number(text)
    client = None
    if cn:
        client = db.query(Client).filter(Client.company_number == cn).first()
    if not client:
        m = re.search(r"(?:for|with)\s+(.+?)(?:\s+due|\s+next|\s+on\s+|$)", text, re.I)
        if m:
            needle = m.group(1).strip().strip("\"'")
            client = (
                db.query(Client)
                .filter(Client.company_name.ilike(f"%{needle}%"))
                .first()
            )
    if not client:
        return {
            "kind": "clarify",
            "reply": "Which client is this task for? Give a company name or number.",
        }

    due = None
    for phrase in ("next friday", "next monday", "tomorrow", "friday", "this friday"):
        if phrase in low:
            due = resolve_relative_date(phrase)
            break
    if not due:
        due = resolve_relative_date("next friday")

    title = f"Follow up — {client.display_name()}"
    m = re.search(r"task\s+(?:to\s+|for\s+)?[\"']?(.+?)[\"']?(?:\s+due|\s+for\s+next|\s+next\s+|$)", text, re.I)
    if m and len(m.group(1)) > 3 and "client" not in m.group(1).lower():
        cand = m.group(1).strip()
        if cand.lower() not in (client.company_name or "").lower():
            title = cand[:120]

    plan = PendingPlan(
        summary=f"Task for {client.display_name()} · due {due.isoformat() if due else '—'}",
        steps=[
            PlanStep(
                op="create_task",
                label="Create task",
                detail=f"{title} · {client.display_name()}",
                params={
                    "title": title,
                    "due_on": due.isoformat() if due else None,
                    "client_id": client.id,
                },
            )
        ],
        preview={
            "company_name": client.display_name(),
            "company_number": client.company_number,
            "task": {"title": title, "due_on": due.isoformat() if due else None},
        },
    )
    # Pre-set client in step via ensure — create_task uses client_id in params
    reply = (
        f"Create task **{title}** for **{client.display_name()}**"
        f"{' · due ' + due.strftime('%d %b %Y') if due else ''}?\n"
        "Nothing is saved until you confirm."
    )
    return _plan_response(plan, reply)


def _llm_chat(
    db: Session,
    message: str,
    history: List[dict],
    page_context: dict,
) -> dict:
    """Call xAI chat completions; interpret plan_request JSON or plain reply."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-8:]:
        role = h.get("role") or "user"
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = (h.get("content") or "")[:4000]
        if content:
            messages.append({"role": role, "content": content})
    ctx_bits = []
    if page_context.get("path"):
        ctx_bits.append(f"page={page_context.get('path')}")
    if page_context.get("client_id"):
        ctx_bits.append(f"client_id={page_context.get('client_id')}")
    user_content = message
    if ctx_bits:
        user_content = f"[{', '.join(ctx_bits)}]\n{message}"
    messages.append({"role": "user", "content": user_content})

    # Read tools — CRM first, then CH when needed
    tools = [
        {
            "type": "function",
            "function": {
                "name": "find_client",
                "description": "Search Accologise CRM clients by name or company number (preferred before Companies House)",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_client_people",
                "description": "List people/directors linked to a CRM client id",
                "parameters": {
                    "type": "object",
                    "properties": {"client_id": {"type": "integer"}},
                    "required": ["client_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ch_profile",
                "description": "Fetch Companies House company profile by number — only if user asked for CH or CRM has no match",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "company_number": {"type": "string"},
                    },
                    "required": ["company_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ch_search",
                "description": "Search Companies House by name — only when user asked for CH or CRM empty",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_overdue_jobs",
                "description": "List overdue open jobs in the CRM",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

    data = _xai_chat(messages, tools=tools)
    # Tool loop (max 3)
    for _ in range(3):
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        messages.append(msg)
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _run_read_tool(db, name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or name,
                    "content": json.dumps(result)[:12000],
                }
            )
        data = _xai_chat(messages, tools=tools)

    choice = (data.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        return {"kind": "message", "reply": "No response from the model — try rephrasing."}

    # Try parse plan_request JSON
    plan_req = _extract_json_object(content)
    if plan_req and plan_req.get("kind") == "plan_request":
        return _plan_from_llm_intent(db, plan_req)

    return {"kind": "message", "reply": content}


def _plan_from_llm_intent(db: Session, plan_req: dict) -> dict:
    intent = (plan_req.get("intent") or "").strip().lower()
    payload = plan_req.get("payload") or {}

    if intent == "navigate":
        href = (payload.get("href") or "").strip()
        label = (payload.get("label") or href or "screen").strip()
        if href.startswith("/"):
            return {
                "kind": "message",
                "reply": f"Opening **{label}**.",
                "links": [{"label": label, "href": href}],
                "navigate": href,
            }
        from app.services.assistant_crm_logic import match_screen

        hit = match_screen(label + " " + (payload.get("screen") or ""))
        if hit:
            return {
                "kind": "message",
                "reply": f"Opening **{hit['label']}**.",
                "links": [{"label": hit["label"], "href": hit["href"]}],
                "navigate": hit["href"],
            }
        return {"kind": "clarify", "reply": "Where should I go? e.g. WIP, jobs, people, sales."}

    if intent == "open_job":
        jid = payload.get("job_id")
        if jid:
            return {
                "kind": "message",
                "reply": f"Opening **job #{jid}**.",
                "links": [{"label": f"Job #{jid}", "href": f"/jobs/{jid}"}],
                "navigate": f"/jobs/{jid}",
            }

    if intent == "open_client":
        name = (payload.get("client_name") or payload.get("company_name") or "").strip()
        cn = (payload.get("company_number") or "").strip()
        synthetic = f"open client for {name or cn}".strip()
        return _try_open_client(db, synthetic) or _try_crm_lookup(db, f"find {name or cn}") or {
            "kind": "clarify",
            "reply": "Which CRM client should I open?",
        }

    if intent == "create_job":
        jtype = (payload.get("type") or "Accounts").strip()
        name = (payload.get("client_name") or payload.get("company_name") or "").strip()
        person = (payload.get("person_name") or "").strip()
        bits = [f"create {jtype} job for {name}"]
        if person:
            bits = [f"create SAR for {person} of {name}"]
        elif "self" in jtype.lower() or jtype.upper() in ("SA", "SAR"):
            if payload.get("for_director"):
                bits = [f"create SAR for director of {name}"]
        got = _try_job_heuristic(db, " ".join(bits))
        if got:
            return got
        return {
            "kind": "clarify",
            "reply": "Which CRM company (and director, for SAR) is this job for?",
        }

    if intent in ("update_job", "fill_job_dates", "edit_job"):
        jid = payload.get("job_id")
        synthetic = "fill dates"
        if jid:
            synthetic = f"fill dates on job #{jid}"
        elif payload.get("type"):
            synthetic = f"fill {payload.get('type')} dates"
        if payload.get("client_name"):
            synthetic += f" for {payload['client_name']}"
        got = _try_fill_job_dates(db, synthetic)
        if got:
            return got
        return {
            "kind": "clarify",
            "reply": "Which job should I update? e.g. fill SAR dates on job #123",
        }

    if intent in ("onboard", "create_prospect", "create_client"):
        # Prefer CRM if already on the book
        name = (payload.get("company_name") or payload.get("client_name") or "").strip()
        cn = normalize_company_number(payload.get("company_number") or "") or extract_company_number(
            json.dumps(payload)
        )
        if name or cn:
            existing = _search_crm_clients(db, cn or name, limit=3)
            if existing and not payload.get("pull_ch") and not payload.get("force_ch"):
                c = existing[0]
                return {
                    "kind": "message",
                    "reply": (
                        f"**{c.display_name()}** is already in the CRM "
                        f"({c.company_number or 'no number'}). "
                        "Open the client, or say create Accounts/SAR job for them. "
                        "Say **pull Companies House** only if you want a CH refresh / new prospect flow."
                    ),
                    "links": _client_links(c),
                }
        if not cn and not name:
            return {
                "kind": "clarify",
                "reply": "I need a company name (CRM) or a company number for Companies House onboarding.",
            }
        if cn:
            synthetic = (
                f"Create a prospect for {name or 'company'}, company number {cn}, "
                f"pull Companies House"
            )
            if payload.get("create_contacts", True):
                synthetic += ", directors as contacts"
            if payload.get("task_due") or payload.get("task_title"):
                synthetic += f", task {payload.get('task_due') or 'next Friday'}"
            got = _try_onboard_heuristic(db, synthetic)
            if got:
                return got
        return {
            "kind": "clarify",
            "reply": "For new CH onboarding I need a company number. For existing clients, name them as in the CRM.",
        }

    if intent == "create_task":
        bits = ["create task"]
        if payload.get("title"):
            bits.append(payload["title"])
        if payload.get("client_name"):
            bits.append(f"for {payload['client_name']}")
        if payload.get("company_number"):
            bits.append(f"company number {payload['company_number']}")
        if payload.get("due_on"):
            bits.append(f"due {payload['due_on']}")
        return _try_task_heuristic(db, " ".join(bits)) or {
            "kind": "clarify",
            "reply": "Which client should the task be for?",
        }

    if intent == "add_note":
        body = (payload.get("body") or "").strip()
        cn = normalize_company_number(payload.get("company_number") or "")
        name = (payload.get("client_name") or "").strip()
        client = None
        if cn:
            client = db.query(Client).filter(Client.company_number == cn).first()
        if not client and name:
            client = (
                db.query(Client)
                .filter(Client.company_name.ilike(f"%{name}%"))
                .first()
            )
        if not client or not body:
            return {
                "kind": "clarify",
                "reply": "I need a client and the note text.",
            }
        plan = PendingPlan(
            summary=f"Add note to {client.display_name()}",
            steps=[
                PlanStep(
                    op="add_client_note",
                    label="Add client note",
                    detail=body[:120],
                    params={"client_id": client.id, "body": body},
                )
            ],
            preview={"company_name": client.display_name()},
        )
        return _plan_response(
            plan,
            f"Add this note to **{client.display_name()}**?\n\n{body}\n\nConfirm to save.",
        )

    return {
        "kind": "message",
        "reply": plan_req.get("message")
        or "I prepared a request but need more detail. Try a clearer command.",
    }


def _run_read_tool(db: Session, name: str, args: dict) -> Any:
    if name == "find_client":
        hits = _search_crm_clients(db, args.get("q") or "", limit=8)
        return {
            "ok": True,
            "source": "crm",
            "items": [
                {
                    "id": c.id,
                    "name": c.display_name(),
                    "company_number": c.company_number,
                    "status": c.overall_status,
                    "href": f"/clients/{c.id}",
                }
                for c in hits
            ],
        }
    if name == "list_client_people":
        cid = int(args.get("client_id") or 0)
        c = db.query(Client).filter(Client.id == cid).first() if cid else None
        if not c:
            return {"ok": False, "error": "client not found"}
        people = []
        for p in c.people or []:
            people.append(
                {
                    "id": p.id,
                    "name": p.display_name(),
                    "role": p.role,
                }
            )
        return {"ok": True, "client": c.display_name(), "people": people}
    if name == "ch_profile":
        cn = normalize_company_number(args.get("company_number") or "")
        if not cn:
            return {"ok": False, "error": "company_number required"}
        res = fetch_company_profile(cn)
        if not res.ok:
            return {"ok": False, "error": res.error}
        data = res.profile or {}
        summary = summarize_profile_dates(data)
        return {"ok": True, "source": "companies_house", "summary": summary, "company_number": cn}
    if name == "ch_search":
        res = search_companies(args.get("q") or "", items_per_page=5)
        if not res.ok:
            return {"ok": False, "error": res.error}
        items = (res.profile or {}).get("items") or []
        return {
            "ok": True,
            "source": "companies_house",
            "items": [
                {
                    "name": it.get("title") or it.get("company_name"),
                    "company_number": it.get("company_number"),
                    "status": it.get("company_status"),
                }
                for it in items[:5]
            ],
        }
    if name == "list_overdue_jobs":
        q = _try_read_query(db, "show overdue jobs")
        return {"ok": True, "reply": (q or {}).get("reply")}
    return {"ok": False, "error": f"unknown tool {name}"}


def _xai_chat(messages: list, tools: Optional[list] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        body["tools"] = tools
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            "https://api.x.ai/v1/chat/completions",
            headers=headers,
            json=body,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"xAI HTTP {r.status_code}: {r.text[:400]}")
        return r.json()


def _extract_json_object(text: str) -> Optional[dict]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None
