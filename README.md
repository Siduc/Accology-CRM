# Accountant CRM

Practice CRM: clients, people, jobs, Companies House job creation, imports, fee schedules, and dashboards.

**Stack:** FastAPI · SQLAlchemy · Jinja2 · SQLite (local) / PostgreSQL (production)

---

## Local development

```bash
pip install -r requirements.txt
copy .env.example .env
# edit .env — set AUTH_USERNAME / AUTH_PASSWORD
python run.py
```

Open http://127.0.0.1:8000  

Login credentials are read from environment variables (loaded from `.env` via `python-dotenv` at startup). If `.env` is missing, development falls back to `accountant` / `password123`.

---

## Production (Render.com)

### Option A — Blueprint (`render.yaml`)

1. Push this repo to GitHub/GitLab.
2. In Render: **New → Blueprint** → select the repo.
3. After deploy, open the web service → **Environment** and set:
   - `AUTH_USERNAME`
   - `AUTH_PASSWORD`
   - Optional: `COMPANIES_HOUSE_API_KEY`
4. `SESSION_SECRET` and `DATABASE_URL` are created automatically when using the blueprint.
5. Health check: `https://your-app.onrender.com/health`

### Option B — Manual web service

| Setting | Value |
|---------|--------|
| Runtime | Python |
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health path | `/health` |

Add a **PostgreSQL** database and link `DATABASE_URL` to the web service.

### Required environment variables (production)

| Variable | Required | Notes |
|----------|----------|--------|
| `ENV` | Yes | Must be `production` |
| `DATABASE_URL` | Yes | Postgres URL (Render injects via blueprint). App normalises `postgres://` → `postgresql+psycopg://` and adds `sslmode=require`. |
| `AUTH_USERNAME` | Yes | Login username |
| `AUTH_PASSWORD` | Yes | Login password |
| `SESSION_SECRET` | Yes | Long random string (session cookie signing) |
| `COMPANIES_HOUSE_API_KEY` | No | REST API key for CH features |
| `PORT` | Auto | Set by Render |

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

---

## Health check

`GET /health` → `{ "status": "ok", "version": "...", "database": true }`

Unauthenticated (for load balancers).

---

## Security notes

- Production refuses to start without `DATABASE_URL` and auth secrets.
- Login uses signed session cookies (`Secure` in production).
- Routes require a session except `/`, `/login`, `/logout`, `/health`, `/static/*`.
- Do not commit `.env`, `crm.db`, or `companies_house_api_key.txt`.

---

## Features (unchanged)

Clients, people, jobs (Accounts / CS / Lost), Companies House job pull, CSV/Excel imports, prior job analysis import, fee schedules, dashboard drill-down.
