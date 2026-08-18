# Local book + Render publish

Laptop does the daily work on `crm.db`. Render is the phone / demo / later client copy.

## Switch (once)

1. Pull live Render → local (backs up current `crm.db` first):

   ```powershell
   cd C:\Users\User\accountant-crm
   $env:CONFIRM_PULL = "YES"
   $env:PYTHONPATH = "."
   python scripts\pull_render_book_to_local.py
   ```

2. In `.env` comment out `DATABASE_URL`. Leave `RENDER_DATABASE_URL` set.

3. Restart `start.bat`. Log must say `sqlite` / `crm.db`, not `ohio-postgres` / `frankfurt`.

4. Enable the 17:00 publish:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_5pm_render_push.ps1 -Enable
   ```

Do daily work only on the X1. Phone/demo catch up at 17:00 (or next login if the lid was shut).

## Frankfurt Postgres

Render has **no London region**. EU = **Frankfurt**.

The web app blueprint is already `region: frankfurt`. The current database host is **Ohio**. That is why the site feels slow even before the laptop hop.

You cannot move a Render database’s region. Create a **new** Postgres in Frankfurt, copy data, point the web service at it:

1. Render → New → Postgres → region **Frankfurt** (same plan as now).
2. From Ohio Postgres: **Backup** or `pg_dump` the External URL.
3. Restore into the Frankfurt External URL (`pg_restore` / `psql`).
4. Web service **accology-crm-1** → Environment → `DATABASE_URL` = Frankfurt **Internal** URL (same region).
5. On this laptop, put the Frankfurt **External** URL in `.env` as `RENDER_DATABASE_URL`.
6. Delete the Ohio database only after a successful phone login on the live site.

Until that exists, 17:00 still publishes to Ohio. After the move, only the URL in `.env` changes.
