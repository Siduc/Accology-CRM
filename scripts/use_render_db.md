# Use the same database as Render (local + live)

## Why numbers differ today

| Where | Database |
|-------|----------|
| Local (default) | SQLite file `crm.db` on this PC |
| Render live site | Postgres on Render |

They are **two copies**. Changing bank opening balance on the website does not change `crm.db`.

## One shared book (recommended)

1. Open **Render** → your **Postgres** service (not the web service).
2. **Connect** → copy **External Database URL**  
   (must say External — Internal only works *inside* Render).
3. Put it in project `.env` (never commit `.env`):

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
```

4. Restart local:

```powershell
cd C:\Users\SimonDuckworth\accountant-crm
$env:PYTHONPATH = "."
# do NOT clear DATABASE_URL
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

5. Check the startup log:

```text
dialect=postgresql host=dpg-….render.com source=DATABASE_URL
```

If you see `sqlite_default` / `crm.db`, the URL is missing or still commented out.

## Safety

- **Same DB** means deletes and imports on local affect the live site.
- Prefer practice mode for chase emails (`CHASE_LIVE_MODE=false`).
- Keep a backup: Render dashboard → Postgres → Backups, or periodic dump.

## Switch back to local SQLite only

Comment out `DATABASE_URL` in `.env` and restart. `crm.db` is used again (old local book).

## Scripts

- `push_local_book_to_render.py` — copy SQLite → Postgres (when they were separate).
- `pull_bank_from_render.py` — copy bank accounts only SQLite ← Postgres.

With a shared `DATABASE_URL` you usually do not need those scripts.
