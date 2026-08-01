"""
Accologise CRM domain logic for Si.

This is the playbook Si follows: screens, job types, dates, who can own a SAR,
and what “should” happen next — matching the app’s own services/UI rules.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Screen map — anything a user can open from main nav / common deep links
# ---------------------------------------------------------------------------

CRM_SCREENS: List[Tuple[Tuple[str, ...], str, str]] = [
    # (keywords), href, label
    (("dashboard", "home", "main screen", "start"), "/dashboard", "Dashboard"),
    (("wip", "work in progress", "working capital wip"), "/working-capital/wip", "WIP"),
    (("working capital", "wc home"), "/dashboard#working-capital", "Working capital"),
    (("debtors", "aged debtors", "sales ledger age"), "/working-capital/debtors", "Debtors"),
    (("creditors", "purchase ledger"), "/working-capital/creditors", "Creditors"),
    (("cash", "cash position"), "/working-capital/cash", "Cash"),
    (("jobs list", "all jobs", "job list", "open jobs"), "/jobs", "Jobs"),
    (("accounts jobs", "accounts list"), "/jobs/accounts", "Accounts jobs"),
    (("sar jobs", "self assessment jobs", "sa jobs"), "/jobs/self-assessment", "SAR jobs"),
    (("other jobs",), "/jobs/other", "Other jobs"),
    (("cs jobs", "confirmation statement jobs"), "/jobs/confirmation-statements", "CS jobs"),
    (("job completion", "completion list", "bill jobs"), "/jobs/completion", "Job completion"),
    (("lost jobs",), "/lost/jobs", "Lost jobs"),
    (("companies", "clients list", "client list", "company list"), "/clients", "Companies"),
    (("people", "contacts list", "directors list"), "/people", "People"),
    (("lost clients", "inactive clients"), "/lost/clients", "Lost clients"),
    (("add client", "new client", "create client screen"), "/clients/new", "Add client"),
    (("add person", "new person"), "/people/new", "Add person"),
    (("new job", "add job", "create job screen"), "/jobs/new", "New job"),
    (("prospects", "prospecting", "pipeline"), "/prospecting", "Prospects"),
    (("prospect list",), "/prospecting/prospects", "Prospect list"),
    (("campaigns",), "/prospecting/campaigns", "Campaigns"),
    (("tasks", "task ledger", "task list"), "/tasks", "Tasks"),
    (("new task", "add task"), "/tasks/new", "New task"),
    (("documents", "docs", "files"), "/documents", "Documents"),
    (("sales", "invoices", "sales home"), "/sales", "Sales"),
    (("quotes",), "/sales/quotes", "Quotes"),
    (("payments", "sales payments"), "/sales/payments", "Payments"),
    (("chase", "debt chase"), "/sales/chase", "Debt chase"),
    (("bank", "bank accounts"), "/bank", "Bank"),
    (("purchase", "bills", "suppliers"), "/purchase", "Purchase"),
    (("vat", "vat return"), "/vat", "VAT"),
    (("groups", "group board"), "/groups", "Groups"),
    (("notes", "scrapbook", "post-its"), "/notes", "Notes"),
    (("asana",), "/asana", "Asana"),
    (("settings",), "/settings", "Settings"),
    (("services", "fee catalogue", "fees"), "/services", "Services"),
    (("companies house jobs", "from companies house", "ch jobs"), "/companies-house/jobs", "Jobs from CH"),
]

# ---------------------------------------------------------------------------
# Domain rules (summarised for Si / LLM)
# ---------------------------------------------------------------------------

CRM_PLAYBOOK = """
You are **Si**, Accologise practice assistant. You operate the CRM the same way a staff user would.

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


def match_screen(utterance: str) -> Optional[Dict[str, str]]:
    """Return {href, label} if utterance is clearly navigation."""
    low = (utterance or "").lower().strip()
    if not low:
        return None
    # Prefer longer keyword matches
    best = None
    best_len = 0
    for keys, href, label in CRM_SCREENS:
        for k in keys:
            if k in low and len(k) > best_len:
                best = {"href": href, "label": label}
                best_len = len(k)
    return best


def job_type_rules_brief(job_type: str) -> str:
    jt = (job_type or "").lower()
    if "self" in jt or jt in ("sa", "sar"):
        return "SAR: person only; PE 5 April; due 31 Jan following (not CT +90/+120)."
    if "account" in jt or jt == "ct":
        return "Accounts/CT: company client; statutory from period end via calculate_dates."
    if "confirmation" in jt or jt == "cs":
        return "CS: company; statutory ≈ PE + 14 days."
    return "Use calculate_dates for this job type; never invent fees."
