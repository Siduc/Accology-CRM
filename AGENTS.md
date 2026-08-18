# Accologise — practice automation

Accologise is the orchestrator. IRIS Elements stays the accounts-production and filing engine.
Excel working papers sit between the books and IRIS.

## Job path (accounts)

1. Retrieve books from source (mostly Xero; also Sage, QBO, bank CSV).
2. Drop source files in the client’s `Current/Source` folder.
3. Produce / update Excel working papers in `Current/Working Papers`.
4. Draft journals in `Current/Journals` and post them back to the source after review.
5. Export an IRIS-ready trial balance to `Current/IRIS Import`.
6. Import that TB into IRIS Elements (Excel first, then IRIS).
7. Client approves accounts / tax.
8. IRIS files at Companies House and HMRC.

Do not invent figures. Confirm before posting journals or filing.

## Folder contract (live clients)

```
Accologise Documents / Clients / {Client}/
  AGENTS.md
  Current/
    Source/
    Working Papers/     this year
    Journals/
    IRIS Import/
    YYYY/               prior-year working papers
  Accounts/
  Tax Return/
  ID-KYC/
  Correspondence/
  Working Papers/       leftovers / un-dated
  Invoices/
  Engagement Letter/
```

Lost clients stay under `Accologise Documents / Lost Clients / {Client}/`.

## Per-client playbook

Each live client has a CRM **Playbook** tab. Saving it writes `AGENTS.md` in that client’s folder.
That file is the client-specific brief: source, year end, IRIS code, approver, quirks.

Do **not** put per-client playbooks in this git repo.

## Xero / Sage / QuickBooks (built)

Settings has three Connect buttons. Pull writes into `Current/Source`. Journals still need an on-screen confirm.

Sage 50 desktop has no cloud login — drop a trial balance CSV into `Current/Source`.

See `TOMORROW-YOUR-ACTIONS.md` for the human checklist.

## Xero (built)

1. Settings → Xero → Connect the practice login (needs `XERO_CLIENT_ID` / `SECRET` / redirect URI).
2. Client → Playbook → link the Xero organisation.
3. Pull from Xero → `Current/Source` (trial balance, P&L, balance sheet, chart, bank, manuals).
4. Drop a journal CSV in `Current/Journals` (Date, Narration, AccountCode, Description, Debit, Credit).
5. Review on screen, tick confirm, post as **Draft** (or Posted).

Do not post journals without that confirm step.

## What is not built yet

- Sage / QBO connectors
- Structured Excel pack generator
- IRIS CSV map + optional UI automation
- Client approval link in the CRM
- Official Xero MCP on this machine (CRM pull/post is the first path)

## X1 vs Render

Daily work and folder creation run on the X1 against local OneDrive.
Render is phone / demo / later client access. It cannot see `C:\Users\...\OneDrive`.
