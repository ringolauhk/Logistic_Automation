# Transfer Note Packing List — build plan

Incremental, feature-flagged delivery. Each build keeps the invoice
workflow untouched and ships with offline tests.

## Build 1 — workflow foundation and upload shell (this build)

- Workflow selector (invoice default; transfer hidden unless
  `TRANSFER_WORKFLOW_ENABLED=true`).
- Transfer upload screen: ordered multi-PDF upload, deterministic
  validation with machine codes, per-file status table.
- Separate `transfer_packing` job type, id format (`tjob-…`), storage root,
  and atomic `transfer_job.json` metadata; explicit persisted upload
  sequence; refresh recovery.
- No OCR, AI, API-Gateway, or Excel logic.

## Build 2 — Transfer Note extraction (planned)

- Extract product/carton rows from stored PDFs (page order within each
  file, file order across the job).
- Progress/status protocol and worker reuse patterned on the invoice
  pipeline's safe-events design; cancellation wired to the existing
  framework.

## Build 3 — product enrichment via API Gateway (planned)

- Access-token acquisition, then `pluLabel-get` lookups: EAN primary,
  Item + Color + Size fallback.
- Analysis Code 01–15 and Composition #1–4 captured per item.
- Offline tests against a loopback mock gateway only.

## Build 4 — grouping, carton renumbering, packing lists (planned)

- Group by To Loc.; carton numbers restart at 001 per destination.
- Identical item/color/size rows combine only within a carton; identical
  products stay separate across cartons.
- One Excel workbook and one delivery invoice number per destination;
  configurable customer mappings.
- Download surface via the existing artifact-allowlist pattern; retention
  cleanup for transfer jobs.
