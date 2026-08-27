# Accologise — practice automation

Accologise is the orchestrator. IRIS Elements stays the accounts-production and filing engine.
Excel working papers sit between the books and IRIS.

Do not invent figures. Confirm before posting journals or filing.
Never modify live client data, production databases, or external filings without explicit human approval.

## Job path (accounts)

1. Retrieve books from source (Xero / Sage Business Cloud / QBO / bank CSV / client TB).
2. Drop source files in the client’s `Current/Source` folder.
3. Produce / update Excel working papers in `Current/Working Papers`.
4. Draft journals in `Current/Journals` and post them back to the source after review.
5. Export an IRIS-ready trial balance to `Current/IRIS Import`.
6. Import that TB into IRIS Elements (Excel first, then IRIS).
7. Client approves accounts / tax (today: email/offline; signed CRM link not built yet).
8. IRIS files at Companies House and HMRC.

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

## Books connectors

Settings has Connect buttons for Xero, Sage Business Cloud, and QBO. Pull writes into `Current/Source`. Journals still need an on-screen confirm.

| Source | Status |
|--------|--------|
| Xero | Built. Practice OAuth; Accology Limited dual-run (CRM invoices, Xero bank from 2026-07-09). |
| Sage Business Cloud | Built. OAuth + pull. |
| Sage 50 desktop | CSV only (drop a trial balance into `Current/Source`). |
| QBO | Connector + Settings Connect; `QBO_CLIENT_ID` / `SECRET` still empty. |

Do not post journals without the confirm step.

## IRIS

TB remap to Accology Chart names; credits negative, net 0; drop file in `Current/IRIS Import`. No IRIS API. Do not automate IRIS filing.

## X1 vs Render

Daily work and folder creation run on the X1 against local OneDrive.
Render is phone / demo / later client access. It cannot see `C:\Users\...\OneDrive`.
Leave `DATABASE_URL` unset on the X1. Use `RENDER_DATABASE_URL` only for the 17:00 publish script.

## Tests

`pytest` uses a throwaway SQLite file. It must never open `crm.db`.
