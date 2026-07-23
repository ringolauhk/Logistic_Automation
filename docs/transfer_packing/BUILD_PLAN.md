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

## Build 2 — Transfer Note extraction (this build)

- Per-page classification (embedded text first; local OCR fallback via the
  optional RapidOCR dependency; per-page failures isolated).
- Deterministic recognition + header/carton/line parsing with raw values
  preserved beside normalized ones; exact carton/document total validation.
- Atomic, schema-versioned `extraction/result.json`; validated job-state
  machine; safe synchronous retry; refresh recovery.
- No API Gateway, no `pluLabel-get`, no carton resequencing, no Excel.

## Build 3 — extraction review, correction, and approval (this build)

- Separate immutable-source review artifact (`review/review.json`) with
  frozen originals, explicit corrections/clears, reasoned exclusions with
  evaluation-time cascade, and a full audit trail (`local-user` pilot).
- Deterministic issue resolution + lookup-readiness (EAN primary,
  Item+Color+Size fallback); recalculated totals; approval gates ending in
  `READY_FOR_PRODUCT_LOOKUP`; extraction-checksum staleness protection.
- No API Gateway, no `pluLabel-get`, no resequencing, no consolidation,
  no Excel.

## Build 4 — product enrichment via API Gateway (planned)

- Access-token acquisition, then `pluLabel-get` lookups: EAN primary,
  Item + Color + Size fallback.
- Analysis Code 01–15 and Composition #1–4 captured per item.
- Offline tests against a loopback mock gateway only.

## Build 5 — grouping, carton renumbering, packing lists (planned)

- Group by To Loc.; carton numbers restart at 001 per destination.
- Identical item/color/size rows combine only within a carton; identical
  products stay separate across cartons.
- One Excel workbook and one delivery invoice number per destination;
  configurable customer mappings.
- Download surface via the existing artifact-allowlist pattern; retention
  cleanup for transfer jobs.
