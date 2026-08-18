"""
Execute confirmed Accologise AI plans via existing CRM services.

Never invent Companies House data — officers/profile come from the signed plan
(populated earlier from real CH API calls) or are re-fetched by number.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.job import Job
from app.models.prospecting import Prospect
from app.services.assistant_plans import PendingPlan, resolve_relative_date
from app.services.company_numbers import normalize_company_number
from app.services.cs_automation import create_contact_from_officer
from app.services.dates import calculate_dates, default_period_end
from app.services.fees import get_suggested_fee
from app.services.practice_tasks import create_task
from app.services.prospecting import create_prospect, enrich_prospect_from_ch

logger = logging.getLogger("accountant_crm.assistant_actions")


def execute_plan(db: Session, plan: PendingPlan) -> Dict[str, Any]:
    """
    Run all steps in order. Returns dict with reply, links, errors.
    Partial success is reported; we do not silently invent data.
    """
    ctx: Dict[str, Any] = {
        "prospect_id": None,
        "client_id": None,
        "task_id": None,
        "job_id": None,
        "people_created": 0,
        "notes": [],
    }
    links: List[Dict[str, str]] = []
    errors: List[str] = []

    for step in plan.steps:
        op = (step.op or "").strip()
        params = dict(step.params or {})
        try:
            if op == "create_prospect":
                _op_create_prospect(db, params, ctx, plan)
            elif op == "enrich_prospect_ch":
                _op_enrich_prospect(db, params, ctx)
            elif op == "ensure_client":
                _op_ensure_client(db, params, ctx, plan)
            elif op == "create_client":
                _op_create_client(db, params, ctx, plan)
            elif op == "create_contacts_from_officers":
                _op_contacts(db, params, ctx, plan)
            elif op == "create_task":
                _op_create_task(db, params, ctx, plan)
            elif op == "create_job":
                _op_create_job(db, params, ctx, plan)
            elif op in ("update_job", "fill_job_dates", "edit_job", "set_job_status"):
                _op_update_job(db, params, ctx, plan)
            elif op == "add_client_note":
                _op_add_note(db, params, ctx)
            elif op == "create_person":
                _op_create_person(db, params, ctx, plan)
            elif op == "navigate":
                href = (params.get("href") or "").strip()
                if href.startswith("/"):
                    ctx["navigate"] = href
                    ctx["notes"].append(f"Open {params.get('label') or href}")
            else:
                errors.append(f"Unknown step: {op}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("assistant step %s failed", op)
            errors.append(f"{step.label or op}: {exc}")

    # Build links + auto-open the thing we just made
    navigate = ctx.get("navigate")
    if ctx.get("prospect_id"):
        links.append(
            {
                "label": "Open prospect",
                "href": f"/prospecting/prospects/{ctx['prospect_id']}",
            }
        )
        navigate = navigate or f"/prospecting/prospects/{ctx['prospect_id']}"
    if ctx.get("client_id"):
        links.append(
            {"label": "Open client", "href": f"/clients/{ctx['client_id']}"}
        )
        if not ctx.get("job_id") and not ctx.get("task_id"):
            navigate = navigate or f"/clients/{ctx['client_id']}"
    if ctx.get("person_id"):
        links.append(
            {"label": "Open person", "href": f"/people/{ctx['person_id']}/edit"}
        )
        navigate = navigate or f"/people/{ctx['person_id']}/edit"
    if ctx.get("task_id"):
        links.append({"label": "Open task", "href": f"/tasks/{ctx['task_id']}/edit"})
        navigate = navigate or f"/tasks/{ctx['task_id']}/edit"
    if ctx.get("job_id"):
        links.append({"label": "Open job", "href": f"/jobs/{ctx['job_id']}"})
        navigate = navigate or f"/jobs/{ctx['job_id']}"

    parts = []
    if ctx.get("prospect_id"):
        parts.append(f"Prospect #{ctx['prospect_id']}")
    if ctx.get("client_id"):
        parts.append(f"Client #{ctx['client_id']}")
    if ctx.get("person_id"):
        parts.append(f"Person #{ctx['person_id']}")
    if ctx.get("people_created"):
        parts.append(f"{ctx['people_created']} contact(s)")
    if ctx.get("task_id"):
        parts.append(f"Task #{ctx['task_id']}")
    if ctx.get("job_id"):
        parts.append(f"Job #{ctx['job_id']}")
    for n in ctx.get("notes") or []:
        parts.append(n)

    if errors and not parts:
        reply = "Could not complete the plan:\n• " + "\n• ".join(errors)
        kind = "message"
    elif errors:
        reply = (
            "Done with some issues.\n"
            + " · ".join(parts)
            + "\n\nIssues:\n• "
            + "\n• ".join(errors)
        )
        kind = "result"
    else:
        reply = "All set. " + (" · ".join(parts) if parts else "Nothing to report.")
        if navigate:
            reply += " Opening it now…"
        kind = "result"

    return {
        "kind": kind,
        "reply": reply,
        "links": links,
        "context": ctx,
        "errors": errors,
        "navigate": navigate,
    }


def _op_create_prospect(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    name = (
        params.get("company_name")
        or (plan.preview or {}).get("company_name")
        or ""
    ).strip()
    cn = normalize_company_number(
        params.get("company_number")
        or (plan.preview or {}).get("company_number")
        or ""
    )
    if not name and not cn:
        raise ValueError("Company name or number required")
    if not name:
        name = cn or "Unnamed"
    p = create_prospect(
        db,
        company_name=name,
        company_number=cn or "",
        notes=params.get("notes") or "Created by Accologise AI",
        source="assistant",
    )
    ctx["prospect_id"] = p.id
    ctx["notes"].append(
        "Linked existing prospect" if params.get("_existing") else "Prospect created"
    )


def _op_enrich_prospect(db: Session, params: dict, ctx: dict) -> None:
    pid = params.get("prospect_id") or ctx.get("prospect_id")
    if not pid:
        raise ValueError("No prospect to enrich")
    p, msg = enrich_prospect_from_ch(db, int(pid))
    if p:
        ctx["prospect_id"] = p.id
    ctx["notes"].append(msg or "CH enrich")


def _op_ensure_client(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    """
    Find or create a Client for contacts/tasks.

    If a prospect exists, link it (client_id) but do **not** mark the prospect
    as won — this is often a live first meeting, not a conversion.
    """
    cn = normalize_company_number(
        params.get("company_number")
        or (plan.preview or {}).get("company_number")
        or ""
    )
    name = (
        params.get("company_name")
        or (plan.preview or {}).get("company_name")
        or ""
    ).strip()
    pid = params.get("prospect_id") or ctx.get("prospect_id")
    prospect = None
    if pid:
        prospect = db.query(Prospect).filter(Prospect.id == int(pid)).first()
        if prospect:
            cn = cn or normalize_company_number(prospect.company_number or "")
            name = name or (prospect.company_name or "")

    client = None
    if cn:
        client = db.query(Client).filter(Client.company_number == cn).first()
    if client:
        ctx["client_id"] = client.id
        ctx["notes"].append(f"Using existing client #{client.id}")
    else:
        if not name and not cn:
            raise ValueError("Cannot create client without name or company number")
        prev = plan.preview or {}
        client = Client(
            company_name=name or cn,
            company_number=cn or None,
            contact_name=prospect.contact_name if prospect else None,
            email=prospect.email if prospect else None,
            phone=prospect.phone if prospect else None,
            address_line1=(
                (prospect.address_line1 if prospect else None)
                or prev.get("address_line1")
            ),
            address_line2=(
                (prospect.address_line2 if prospect else None)
                or prev.get("address_line2")
            ),
            town=(prospect.town if prospect else None) or prev.get("town"),
            postcode=(prospect.postcode if prospect else None)
            or prev.get("postcode"),
            overall_status="Active",
            engagement_date=date.today(),
            source="assistant",
            notes="Created by Accologise AI (meeting)",
        )
        db.add(client)
        db.flush()
        ctx["client_id"] = client.id
        ctx["notes"].append(f"Created client #{client.id}")

    # Link prospect without marking won (live meeting, not conversion)
    if prospect and not prospect.client_id:
        prospect.client_id = client.id
        if (prospect.pipeline_status or "new") == "new":
            prospect.pipeline_status = "contacted"
        db.flush()

    db.commit()
    if client:
        db.refresh(client)


def _op_create_client(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    _op_ensure_client(db, params, ctx, plan)


def _op_contacts(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    cid = params.get("client_id") or ctx.get("client_id")
    if not cid:
        raise ValueError("Client required before creating contacts")
    client = db.query(Client).filter(Client.id == int(cid)).first()
    if not client:
        raise ValueError("Client not found")

    officers = params.get("officers") or (plan.preview or {}).get("officers") or []
    # Re-fetch if empty but we have a company number
    if not officers:
        cn = normalize_company_number(
            params.get("company_number")
            or (plan.preview or {}).get("company_number")
            or client.company_number
            or ""
        )
        if cn:
            from app.services.companies_house import fetch_company_officers

            res = fetch_company_officers(cn)
            if res.ok and res.profile:
                officers = _normalise_officer_list(res.profile)

    count = 0
    for off in officers:
        name = (off.get("name") or off.get("officer_name") or "").strip()
        if not name:
            continue
        # Skip resigned if flagged
        if off.get("resigned") or off.get("resigned_on"):
            continue
        role = (off.get("role") or off.get("officer_role") or "Director").strip()
        person = create_contact_from_officer(
            db, client, officer_name=name, officer_role=role
        )
        if person:
            count += 1
    db.commit()
    ctx["people_created"] = count
    ctx["notes"].append(f"{count} director/officer contact(s)")


def _op_create_task(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    cid = params.get("client_id") or ctx.get("client_id")
    title = (params.get("title") or "Follow-up").strip()
    due_raw = params.get("due_on") or (plan.preview or {}).get("task", {}).get(
        "due_on"
    )
    due: Optional[date] = None
    if isinstance(due_raw, date):
        due = due_raw
    elif due_raw:
        due = resolve_relative_date(str(due_raw))
        if due is None:
            # try ISO already
            try:
                due = date.fromisoformat(str(due_raw)[:10])
            except ValueError:
                due = None

    task = create_task(
        db,
        title=title,
        description=params.get("description") or "Created by Accologise AI",
        client_id=int(cid) if cid else None,
        due_on=due,
        priority=params.get("priority") or "Medium",
        status="Planned",
        notes=params.get("notes") or "Accologise AI",
        import_source="assistant",
    )
    ctx["task_id"] = task.id


def _op_create_job(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    """Create a job on a CRM client. SAR for a director uses person_id → individual shell."""
    cid = params.get("client_id") or ctx.get("client_id")
    jtype = (params.get("type") or "Accounts").strip()

    # SAR for a person/director: attach to individual client shell, not the Ltd
    person_id = params.get("person_id")
    if person_id or (
        jtype in ("Self Assessment", "SA", "SAR") and params.get("for_person")
    ):
        from app.models.person import Person
        from app.services.individuals import ensure_individual_client

        pid = int(person_id or params.get("for_person") or 0)
        person = db.query(Person).filter(Person.id == pid).first() if pid else None
        if not person and params.get("person_name"):
            needle = (params.get("person_name") or "").strip()
            person = (
                db.query(Person)
                .filter(Person.full_name.ilike(f"%{needle}%"))
                .order_by(Person.id)
                .first()
            )
        if person:
            shell = ensure_individual_client(db, person)
            db.commit()
            cid = shell.id
            ctx["client_id"] = shell.id
            ctx["notes"].append(f"SAR client shell for {person.display_name()}")

    if not cid:
        cn = normalize_company_number(params.get("company_number") or "")
        if cn:
            c = db.query(Client).filter(Client.company_number == cn).first()
            if c:
                cid = c.id
                ctx["client_id"] = c.id
    if not cid and params.get("client_name"):
        needle = (params.get("client_name") or "").strip()
        c = (
            db.query(Client)
            .filter(Client.company_name.ilike(f"%{needle}%"))
            .order_by(Client.id)
            .first()
        )
        if c:
            cid = c.id
            ctx["client_id"] = c.id
    if not cid:
        raise ValueError("Client required to create a job — name a company in the CRM")

    # Normalise type labels
    tlow = jtype.lower()
    if tlow in ("sa", "sar", "self assessment", "self-assessment"):
        jtype = "Self Assessment"
    elif tlow in ("cs", "confirmation", "confirmation statement"):
        jtype = "Confirmation Statement"
    elif tlow in ("accounts", "account"):
        jtype = "Accounts"

    # Period end + type-specific dates (SAR ≠ Accounts/CT)
    pe = _parse_period_end(params.get("period_end"))
    if pe is None:
        pe = default_period_end(jtype)
    statutory, t_start, t_comp = calculate_dates(jtype, pe)

    fee = float(params.get("fee") or 0)
    if fee <= 0:
        suggested = get_suggested_fee(db, jtype, pe, client_id=int(cid))
        if suggested is not None:
            fee = float(suggested)

    from app.services.dates import uk_date

    pe_label = uk_date(pe) if pe else ""
    title = (params.get("title") or "").strip()
    if not title:
        title = f"{jtype}" + (f" — {pe_label}" if pe_label else "")

    job = Job(
        title=title,
        type=jtype,
        client_id=int(cid),
        period_end=pe,
        statutory_due_date=statutory,
        target_start=t_start,
        target_completion=t_comp,
        fee=fee,
        status=params.get("status") or "Planned",
        is_recurring="Yes",
        notes=params.get("notes") or "Created by Si",
        source="assistant",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    ctx["job_id"] = job.id
    bits = []
    if pe:
        bits.append(f"PE {pe.isoformat()}")
    if statutory:
        bits.append(f"due {statutory.isoformat()}")
    if bits:
        ctx["notes"].append(f"{jtype} dates: " + ", ".join(bits))


def _parse_period_end(raw) -> Optional[date]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    # tax year labels: 2025/26, 2025-26
    m = re.match(r"^(20\d{2})\s*[/\-]\s*(\d{2})$", s)
    if m:
        y = int(m.group(1))
        return date(y + 1, 4, 5) if int(m.group(2)) == (y + 1) % 100 else date(y, 4, 5)
    # "tax year 2026" / "5 April 2026"
    low = s.lower()
    if "april" in low or "tax year" in low:
        ym = re.search(r"(20\d{2})", s)
        if ym:
            y = int(ym.group(1))
            # "tax year 2025/26" style already handled; bare year → 5 Apr of that year
            return date(y, 4, 5)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return resolve_relative_date(s)


def _op_update_job(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    """Fill or recalculate dates on an existing job (Si can edit)."""
    job = None
    jid = params.get("job_id") or ctx.get("job_id")
    if jid:
        job = db.query(Job).filter(Job.id == int(jid)).first()
    if not job and (params.get("client_id") or ctx.get("client_id")):
        cid = int(params.get("client_id") or ctx.get("client_id"))
        jtype = (params.get("type") or "").strip()
        q = db.query(Job).filter(Job.client_id == cid)
        if jtype:
            q = q.filter(Job.type.ilike(f"%{jtype}%"))
        job = q.order_by(Job.id.desc()).first()
    if not job and params.get("person_id"):
        from app.services.individuals import individual_client_number
        from app.models.person import Person

        person = db.query(Person).filter(Person.id == int(params["person_id"])).first()
        if person:
            ref = individual_client_number(person.id)
            shell = db.query(Client).filter(Client.company_number == ref).first()
            if shell:
                job = (
                    db.query(Job)
                    .filter(Job.client_id == shell.id)
                    .order_by(Job.id.desc())
                    .first()
                )
    if not job:
        raise ValueError("No job found to update — open or name the job/client")

    jtype = (params.get("type") or job.type or "Accounts").strip()
    tlow = jtype.lower()
    if tlow in ("sa", "sar", "self assessment", "self-assessment"):
        jtype = "Self Assessment"
    elif tlow in ("accounts", "account", "ct", "corporation tax"):
        # Only switch type if caller explicitly asked
        if params.get("type"):
            jtype = "Accounts" if "account" in tlow else jtype
    # Keep existing type unless params override with a real job type
    if params.get("type"):
        if tlow in ("sa", "sar", "self assessment", "self-assessment"):
            job.type = "Self Assessment"
            jtype = "Self Assessment"
        elif "confirmation" in tlow:
            job.type = "Confirmation Statement"
            jtype = "Confirmation Statement"
        elif "account" in tlow:
            job.type = "Accounts"
            jtype = "Accounts"
    else:
        jtype = job.type or jtype

    pe = _parse_period_end(params.get("period_end"))
    if pe is None and (params.get("fill_dates") or params.get("recalculate") or not job.period_end):
        pe = job.period_end or default_period_end(jtype)
    if pe is not None:
        job.period_end = pe

    if params.get("fill_dates") or params.get("recalculate") or not job.statutory_due_date:
        statutory, t_start, t_comp = calculate_dates(jtype, job.period_end)
        if statutory:
            job.statutory_due_date = statutory
        # Only set planning dates when empty or force
        if params.get("recalculate") or not job.target_start:
            job.target_start = t_start
        if params.get("recalculate") or not job.target_completion:
            job.target_completion = t_comp

    if params.get("title"):
        job.title = str(params["title"]).strip()
    elif job.period_end and job.type:
        from app.services.dates import uk_date

        job.title = f"{job.type} — {uk_date(job.period_end)}"

    if params.get("fee") is not None:
        try:
            job.fee = float(params["fee"])
        except (TypeError, ValueError):
            pass
    elif not job.fee:
        suggested = get_suggested_fee(
            db, job.type or jtype, job.period_end, client_id=job.client_id
        )
        if suggested is not None:
            job.fee = float(suggested)

    if params.get("notes"):
        extra = str(params["notes"]).strip()
        job.notes = f"{(job.notes or '').strip()}\n{extra}".strip() if job.notes else extra

    if params.get("status"):
        from app.services.dates import JOB_STATUSES as DATE_JOB_STATUSES

        st = str(params["status"]).strip()
        aliases = {
            "complete": "Completed",
            "done": "Completed",
            "in progress": "In Progress",
            "hold": "On hold",
            "on-hold": "On hold",
        }
        st = aliases.get(st.lower(), st)
        allowed = set(DATE_JOB_STATUSES) | set(Job.OPEN_STATUSES) | set(
            Job.CLOSED_STATUSES
        ) | set(Job.HOLD_STATUSES)
        if st in allowed:
            job.status = st
            ctx["notes"].append(f"Status → {st}")

    job.source = job.source or "assistant"
    db.commit()
    db.refresh(job)
    ctx["job_id"] = job.id
    ctx["client_id"] = job.client_id
    ctx["notes"].append(
        f"Updated job #{job.id}: PE {job.period_end or '—'}, "
        f"due {job.statutory_due_date or '—'} ({job.type})"
    )


def _op_create_person(
    db: Session, params: dict, ctx: dict, plan: PendingPlan
) -> None:
    """Create a person/contact, optionally link to a CRM company."""
    from app.models.person import Person
    from app.text_format import normalize_person_name

    name = (params.get("full_name") or params.get("name") or "").strip()
    if not name:
        raise ValueError("Person name required")
    role = (params.get("role") or "Contact").strip() or "Contact"
    person = Person(
        full_name=name,
        email=(params.get("email") or "").strip() or None,
        phone=(params.get("phone") or "").strip() or None,
        role=role,
        person_status="Contact",
        notes=params.get("notes") or "Created by Si",
    )
    cid = params.get("client_id") or ctx.get("client_id")
    if not cid and params.get("client_name"):
        needle = (params.get("client_name") or "").strip()
        hit = (
            db.query(Client)
            .filter(Client.company_name.ilike(f"%{needle}%"))
            .order_by(Client.id)
            .first()
        )
        if hit:
            cid = hit.id
    if cid:
        client = db.query(Client).filter(Client.id == int(cid)).first()
        if client:
            person.clients.append(client)
            ctx["client_id"] = client.id
    db.add(person)
    db.commit()
    db.refresh(person)
    ctx["person_id"] = person.id
    ctx["notes"].append(f"Person {normalize_person_name(name)} created")


def _op_add_note(db: Session, params: dict, ctx: dict) -> None:
    cid = params.get("client_id") or ctx.get("client_id")
    body = (params.get("body") or params.get("note") or "").strip()
    if not body:
        raise ValueError("Note text required")
    if cid:
        client = db.query(Client).filter(Client.id == int(cid)).first()
        if client:
            existing = (client.notes or "").strip()
            stamp = date.today().isoformat()
            addition = f"[{stamp} AI] {body}"
            client.notes = f"{existing}\n{addition}".strip() if existing else addition
            db.commit()
            ctx["notes"].append("Note added to client")
            return
    # Fallback scrap note
    from app.services.scrap_notes import create_note

    create_note(
        db,
        title=params.get("title") or "AI note",
        body=body,
        color="yellow",
        pin_live=False,
    )
    ctx["notes"].append("Scrap note created")


def _normalise_officer_list(profile_or_items: Any) -> List[dict]:
    """CH officers payload → [{name, role, resigned_on}, …]."""
    items = profile_or_items
    if isinstance(profile_or_items, dict):
        items = profile_or_items.get("items") or profile_or_items.get("officers") or []
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        role = (
            it.get("officer_role")
            or it.get("role")
            or "Director"
        )
        out.append(
            {
                "name": name,
                "role": str(role).replace("-", " ").title(),
                "resigned_on": it.get("resigned_on"),
                "resigned": bool(it.get("resigned_on")),
            }
        )
    return out


def build_onboard_plan(
    *,
    company_name: str,
    company_number: str,
    officers: List[dict],
    task_title: str,
    task_due: Optional[date],
    want_contacts: bool = True,
    want_task: bool = True,
    want_prospect: bool = True,
    want_client: bool = True,
    pull_ch: bool = True,
    address: Optional[dict] = None,
) -> PendingPlan:
    """Assemble the standard prospect-meeting plan from validated data."""
    cn = normalize_company_number(company_number) or ""
    name = (company_name or "").strip() or cn or "Company"
    steps: List = []
    from app.services.assistant_plans import PlanStep

    if want_prospect:
        steps.append(
            PlanStep(
                op="create_prospect",
                label="Create prospect",
                detail=f"{name}" + (f" · {cn}" if cn else ""),
                params={"company_name": name, "company_number": cn},
            )
        )
        if pull_ch and cn:
            steps.append(
                PlanStep(
                    op="enrich_prospect_ch",
                    label="Pull Companies House profile",
                    detail=f"Refresh address, SIC, officers for {cn}",
                    params={},
                )
            )
    if want_client or want_contacts or want_task:
        steps.append(
            PlanStep(
                op="ensure_client",
                label="Create / link client",
                detail="Needed so directors and tasks can attach",
                params={"company_name": name, "company_number": cn},
            )
        )
    if want_contacts and officers:
        active = [o for o in officers if not o.get("resigned") and not o.get("resigned_on")]
        steps.append(
            PlanStep(
                op="create_contacts_from_officers",
                label=f"Add {len(active)} director/officer contact(s)",
                detail=", ".join((o.get("name") or "")[:40] for o in active[:8]),
                params={"officers": active, "company_number": cn},
            )
        )
    due_s = task_due.isoformat() if task_due else None
    if want_task:
        steps.append(
            PlanStep(
                op="create_task",
                label="Create follow-up task",
                detail=(task_title or "Follow-up")
                + (f" · due {due_s}" if due_s else ""),
                params={
                    "title": task_title or f"Follow up — {name}",
                    "due_on": due_s,
                },
            )
        )

    preview = {
        "company_name": name,
        "company_number": cn,
        "officers": officers,
        "task": {
            "title": task_title or f"Follow up — {name}",
            "due_on": due_s if want_task else None,
        },
    }
    if address:
        preview.update(address)

    summary_bits = [f"Prospect + Client for {name}"]
    if cn:
        summary_bits[0] += f" ({cn})"
    if want_contacts and officers:
        summary_bits.append(f"{len([o for o in officers if not o.get('resigned_on')])} contacts")
    if want_task:
        summary_bits.append(
            f"task due {task_due.isoformat()}" if task_due else "follow-up task"
        )

    return PendingPlan(
        summary=" · ".join(summary_bits),
        steps=steps,
        preview=preview,
    )
