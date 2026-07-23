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

## Build 4 — API Gateway authentication client (this build)

- Reusable backend-only auth layer per the confirmed v0.851 contract:
  login (`client`/`userId`/`password`/`locale`), refresh (`rt`), envelope
  `code == 100000` validation on top of HTTP status, `expire_in` expiry
  with skew, spec-compliant refresh-token rotation.
- Process-local thread-safe in-memory token cache (no persistence, no
  browser exposure); narrow transport-only retries; one re-login fallback
  after a rejected refresh; typed redacted errors; config-only readiness
  status in the UI. No `pluLabel-get` call exists.

## Build 5 — product enrichment via API Gateway (this build)

- `pluLabel-get` lookups using the Build 4 auth client: EAN primary,
  Item+Color+Size literal-concatenation fallback (repeated color suffix
  kept); deduplicated batched requests correlated by echoed
  (locationCode, plu); one batch retry after 401.
- Analysis Code 01–15 / Composition #1–4 slots with pattern-mapped wire
  names (unconfirmed locally) + lossless token-free raw records;
  source/API comparison with blocking identity mismatches; atomic
  `product_lookup/result.json` guarded by the review checksum.
- No grouping, renumbering, consolidation, invoice numbering, or Excel.

## Build 6 — packing preparation (this build)

- Destination grouping by effective To Loc. (first-appearance order);
  carton order = upload then page order; renumbering restarts at 001 per
  destination with originals kept auditable.
- Same-carton consolidation on authoritative API identity (quantities
  summed, source line IDs traceable); cross-carton/destination merging
  structurally impossible.
- One deterministic delivery invoice number per destination
  (job-scoped uniqueness only in the pilot); atomic, checksum-guarded
  `packing/result.json` with stale archival; no API/Excel/ZIP.

## Build 7 — workbook generation and delivery (planned)

- One Excel workbook per destination from the packing preparation;
  customer Analysis Code / Composition mapping; download surface via the
  artifact-allowlist pattern; retention cleanup for transfer jobs.
