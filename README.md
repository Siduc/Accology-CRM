# Accologise — practice CRM

Practice operating system for Accology / Accologise: clients → jobs → books → working papers → journals → IRIS trial balance → invoice → approval/filing.

**Accologise orchestrates. IRIS Elements files** at Companies House and HMRC after client approval. This app does not file accounts or tax.

**Stack:** Python 3.12–3.14 · FastAPI · SQLAlchemy 2 · Jinja2 (server-rendered) · SQLite locally / PostgreSQL on Render

---

## Source of truth

| Surface | Role |
|---------|------|
| X1 laptop `crm.db` + OneDrive client folders | Daily live book. Type here. |
| Render web + Postgres | Phone / demo / delayed look-only copy. Published ~17:00. Melissa is a viewer; do not type daily on the live site. |
| GitHub `Siduc/Accology-CRM` | Code remote. Must not contain `.env`, `crm.db`, or API key files. |

Local app: http://127.0.0.1:8000 (`start.bat`). Leave `DATABASE_URL` **unset** on the X1 so pages hit local SQLite, not Ohio Postgres.

---

## Local development

```bash
pip install -r requirements.txt
copy .env.example .env
# edit .env — AUTH_USERNAME / AUTH_PASSWORD / SESSION_SECRET
python run.py
```

Or double-click `start.bat`.

Tests (throwaway SQLite, never `crm.db`):

```bash
pytest
```

---

## Production (Render)

Blueprint in `render.yaml`: Python 3.12, `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health `/health`.

Region note: web is Frankfurt; Postgres has historically been Ohio. Moving DB region needs a **new** Frankfurt database + restore, not an in-place change.

Data sync (X1 → Render) is `scripts/push_local_book_to_render.py` using `RENDER_DATABASE_URL`. Pull the other way is `scripts/pull_render_book_to_local.py` with `CONFIRM_PULL=YES`. Those are independent of git push.

Required production env: `ENV=production`, `DATABASE_URL`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `SESSION_SECRET` (≥16 chars).

---

## What is built

- Clients, people, groups, jobs (Accounts, CS, SA, VAT, other), playbooks → per-client `AGENTS.md` on disk (not in git).
- OneDrive folder contract: `Accologise Documents / Clients / {Client}/Current/{Source,Working Papers,Journals,IRIS Import}`.
- Xero practice OAuth: pull into `Current/Source`; journal CSV review + confirm before Draft/Posted.
- Sage Business Cloud OAuth + pull. Sage 50 desktop: drop a TB CSV (no cloud login).
- QBO connector + Settings Connect (needs `QBO_CLIENT_ID` / `SECRET`).
- Sales ledger, quotes, invoices (Accology Limited + Accology Pays), PDF, email-then-Xero-push.
- Bank / purchase / VAT ledgers; payroll onboarding; working-capital dashboard.
- Companies House profile, CS packs, XML Gateway **export** (live submit gated).
- CS Bot handoff: workflow stages, late/`was_late`, code-request stamps, ready+sent invoice or filed draft + £50 CH disbursement (`docs/cs-bot-handoff.md`).
- Microsoft Graph (OneDrive + mail), Asana, post inbox, prospecting, public site + locked demo login.
- In-app assistant (xAI when keyed); writes require confirm.

## What is not built yet

- MFA / role matrix UI (melissa is a `viewer`; Render is look-only. Principal still edits on the X1).
- Client approval email send and draft-PDF on the public page (signed yes/no link is built; staff mint from the job).
- WhatsApp intake, open-banking feed, MFA, emailing the approval link.
- IRIS API or UI automation (still “import CSV in IRIS”).
- QBO first live pull (keys empty). Open-banking feed. WhatsApp intake.
- Companies House XML **live** submit and CH OAuth filing (localhost redirect blocked; needs public HTTPS).
- Automated test coverage beyond the smoke suite in `tests/`.

---

## Security

- Do not commit `.env`, `crm.db`, `companies_house_api_key.txt`, or `backup.json`.
- Production refuses to start without DB URL and auth secrets. `/docs` is off in production.
- `/api/*` never 303s to login; Outlook/task POSTs need `X-API-Key`.
- Demo login is locked: UI anonymised, cannot write live data.
- Journal post and CH XML live submit are gated (`CH_XML_SUBMIT_LIVE`).
- Client rows still have fields for Government Gateway and software passwords — treat as debt; do not copy that DB to extra hosts.

---

## Docker (optional)

```bash
docker build -t accountant-crm .
docker run -p 8000:8000 \
  -e ENV=production \
  -e DATABASE_URL=postgresql+psycopg://... \
  -e AUTH_USERNAME=... \
  -e AUTH_PASSWORD=... \
  -e SESSION_SECRET=... \
  accountant-crm
```
