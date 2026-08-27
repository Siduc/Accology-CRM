# Changelog

## 2026-08-27

- Melissa is a `viewer` login (look-only). Simon (principal) still edits on the X1. Render is a look-only published copy.

## 2026-08-25

- CS introduced product: client `cs_tariff` book|product. Only tagged clients (currently Jacques) get £10+VAT+£50 CH; book stays £50+VAT+£50 CH.
- Practice hold: client-level `on_hold` (reason + note) so CS/accounts/VAT/chase bots skip without marking Inactive. Staff set a reason. See `docs/cs-bot-handoff.md`.
- CS workflow: pack code-request timestamps, board stages (including missing auth / unbilled), `was_late` on late filings, and a draft-invoice safety net when marking filed. See `docs/cs-bot-handoff.md`.
- Playbook: Build draft working papers (Excel in Current/Working Papers) from the source TB. Does not file.
- Settings: staff login list for the principal (hashed; no passwords shown).


- Documented current practice architecture (X1 SQLite live book, Render delayed copy, IRIS files).
- Corrected README / AGENTS: Xero, Sage Business Cloud, and QBO connectors exist; QBO keys still empty.
- Added smoke tests (`tests/`) against throwaway SQLite — health, login, API-not-redirect, gitignore hygiene.
- Tightened `.gitignore` (`*.db`, WAL/SHM, `backup.json`).
- Added GitHub Actions pytest workflow (runs only after a push; no live data).
- Stop storing Government Gateway / accounts-software passwords on client and person rows. Existing values are left in place until you approve a clear-down.
- Added `staff_users` table (PBKDF2 hashes). Seeds the current AUTH_USERNAME as principal; env login still works as fallback. No Melissa password invented.
- Client approval signed link: staff mint at `/jobs/{id}/approval-link`, public `/approve/{token}` (14 days). Logs approved/declined on the job. Does not email or file.
