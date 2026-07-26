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

## Build 7 — packing-list workbook generation (implemented)

- One validated `.xlsx` per destination (five fixed sheets mirroring the
  legacy layout; text-format identifiers; per-carton subtotals; print
  setup) + a validated ZIP for multiple destinations; per-workbook and
  ZIP downloads with stale-input protection and stable regeneration.
- Customer Analysis Code mapping stays configuration-only placeholders;
  printing/email and full transfer-job retention remain future work.

## Build 8 — pilot hardening and controlled live validation (this build)

- Docker OCR packaging: the web image installs the pinned
  `requirements-ocr.txt` stack (plus `libgl1`/`libglib2.0-0`) so scanned
  Transfer Notes OCR inside the container; CLI image unchanged; models
  ship inside the wheel (no runtime downloads).
- `apps/web/transfer/pilot.py`: redaction-safe `doctor` diagnostics
  (exit 0/1/2), transfer-root retention `cleanup` (dry-run default,
  in-progress jobs and invoice jobs protected, symlink/containment safe),
  the redacted `pilot/result.json` manifest, and doubly gated live
  probes (`PILOT_ENABLE_LIVE_*` env flag AND `--yes`): one auth login,
  one product batch of 1–3 approved identifiers, optional Qty comparison.
- Pilot Readiness expander on the transfer page (names/statuses only).
- Docs: `PILOT_CHECKLIST.md`, `PILOT_ROLLBACK.md`, `LIVE_VALIDATION.md`.
- Analysis Code / Composition wire names and the Qty policy remain
  UNCONFIRMED until the gated live probes are approved and executed;
  customer mappings stay blank until business evidence confirms them.
- Not in Build 8: global invoice numbering, print/email automation,
  SSO/RBAC, background queues, merge to main.
