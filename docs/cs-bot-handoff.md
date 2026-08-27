# CS Bot ↔ CRM Systems Lead hand-off

Source of truth: X1 local `crm.db` (`C:\Users\User\accountant-crm`). Render is a delayed copy — do not type CS work there. CRM Systems Lead owns schema and code. Confirmation Statement Bot owns operational CS (due dates, late filings, code chases, pack prep, WebFiling, triggering the bill).

Do not: change schema, live-submit CH XML, invent figures, file accounts/tax, or push GitHub/Render unless Simon asks.

## Entities

- **Job** (`type = Confirmation Statement`): `period_end` = made-up-to date; `statutory_due_date` = +14 days. Generic `status` (Planned / In Progress / Completed / …). Billing on the job: `fee`, `billing_status`, `invoice_reference`, `was_late`.
- **Pack** (`cs_packs`): filing pipeline `draft` → `in_review` → `ready_to_file` → `filed` (or `cancelled`). Linked by `job_id` / `client_id`.
- **Codes**: company auth `clients.ch_authentication_code` (encrypted). Personal CH identification `people.ch_code`. Chase timestamps on the pack: `code_requested_at`, `code_received_at`, `code_request_note` (do not store the code in the note).
- **Board**: `/cs/readiness` — filters `open`, `overdue`, `due_soon`, `ready`, `almost`, `missing`, `missing_auth`, `unbilled`. Derived `workflow_stage`: `awaiting_auth_code` | `awaiting_personal_code` | `code_requested` | `preparing` | `ready_to_file` | `billed` | `filed` | `late`.

## Billing triggers

CS invoices always add £50 Companies House disbursement, 0% VAT (`CH_DISB`).

1. Board **Ready + invoice** / pack Ready checkbox (default on) → **sent** invoice from the linked job.
2. Mark **filed** → sets `job.was_late = yes` if due date is past; if no invoice exists, raises a **draft** invoice (no email). Existing invoices are left alone.
3. Completing the job from the jobs screen can still invoice as before.

## Code chase posts

- `POST /cs/{pack_id}/code-requested` (optional `note`) — stamps `code_requested_at`.
- `POST /cs/{pack_id}/code-received` — stamps `code_received_at`. Does not write the code; put the code on the client/person form as usual.

## CS tariff (book vs introduced product)

Orthogonal to `overall_status` and `on_hold`. Column: `clients.cs_tariff` (`book` | `product`). Default is **book**. Staff set the flag on the client screen — do not auto-default new clients to £10.

- **book**: existing fee / `get_suggested_fee` (typically £50 + 20% VAT + £50 CH_DISB 0% VAT = £110 to the client). Prior-year × 1.05 still applies. Year schedule stays at £50.
- **product**: introduced £10 + 20% VAT + £50 CH_DISB. `get_suggested_fee` returns 10. `with_cs_disbursement` still attaches CH_DISB.

CS Bot MUST use `client.cs_tariff` (or `client.is_cs_product()`). Never assume a new client is £10 unless the flag is `product`. New CS jobs (`_find_or_create_cs_job`) take fee from `get_suggested_fee`. Do not reprice Completed/Cancelled jobs or rewrite invoices already raised. Existing Accology book CS jobs stay on their current fees.

## Practice hold

Orthogonal to `overall_status`. Inactive = lost. Hold = we still have the client, but bots must skip.

- Columns on `clients`: `on_hold` (0/1), `hold_reason`, `hold_note`, `hold_set_at`, `hold_set_by`.
- Reasons: `bad_payer` | `no_submissions` | `disengaging` | `client_request` | `other`.
- Bots MUST call `client.is_on_hold()` / `practice_hold.is_held(client)` before filing, code chase, spawning jobs, raising CS invoices from automation, or debt chase live send.
- CS board (`/cs/readiness`) skips held clients unless `?filter=on_hold`.
- Do not file, chase, or bill CS for held clients. Simon / CS Bot sets holds with a reason - do not invent which live clients are bad payers.

## After a CRM code change

Restart `start.bat` so `init_db` adds any new columns. Schema work is CRM Systems Lead only.
