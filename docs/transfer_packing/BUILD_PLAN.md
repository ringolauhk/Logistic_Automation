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

## Build 9 — live validation and pilot acceptance (this build)

- Controlled live probes executed under explicit approval: auth (one
  login), product schema (one batch), Qty comparison (one batch). Wire
  schema confirmed; see `LIVE_VALIDATION.md`.
- Evidence-based fixes only:
  1. composition wire names arrive misspelled (`compositon1..4`) — the
     Build 5 adapter now accepts them (spelling-tolerant regex);
  2. a reviewed HEADER delivery-note correction now reaches packing
     outputs (TN# remark, carton source keys, line sources) when page
     parsing left line/carton D/N empty;
  3. `PREPARABLE_STATUSES` now includes the workbook states the Build 7
     transition table always allowed, so packing can be re-prepared after
     workbook generation (outputs go stale, invoice numbers stay stable).
- Qty policy A confirmed (Qty stays 1, out of the dedup key).
- Customer mappings remain blank (no confirming evidence).
- One controlled end-to-end Docker pilot on a real 1-page image-only
  Transfer Note: OCR extraction exact, live lookup 1 batch/2 EANs/0
  fallbacks, workbook validated and opened in Excel without repair,
  restart recovery proven, redacted `pilot/result.json` written.
- Not in Build 9: unknown-identifier live test (not approved), global
  invoice numbering, print/email automation, merge to main.
## Build 10 — guided workflow UI, consolidated attributes, Excel export

- The Transfer page is a guided top-to-bottom flow: workflow progress
  indicator (derived solely from the persisted job status), Upload,
  Extraction, Review & approve, Product lookup, Final enriched product
  lines, Packing, Workbooks, downloads - the next action always appears
  directly below the last successful stage (the Build 9 anchor-jump
  controls are gone).
- Progressive disclosure: successful stage details collapse into
  expanders; blocking sections auto-expand; warnings stay collapsed.
- ONE line-based "Final enriched product lines" table replaces the
  separate per-line/attribute tables: source values, API values, prices,
  AC01-AC15 and Composition 1-4 in the same row, fixed schema across
  jobs, column selector + "Show all product attributes" toggle,
  identifiers as text.
- Local Excel export of that table (`enriched_export.py`):
  Transfer_<job_id>_Enriched_Product_Lines.xlsx, worksheet "Enriched
  Product Lines", frozen bold header, AutoFilter, text identifiers with
  leading zeros, numeric quantities/prices, blank attributes stay blank;
  built in memory from the persisted artifact - no API call, no state
  change, no temp files.
- All Build 5-9 protections unchanged (checkpoints, run lock, 429
  handling incl. the exact no-Retry-After wording, restart confirmation,
  staleness, redaction).

- Pilot-hardening follow-up (real 481-lookup job): product lookup is now
  checkpointed and resumable - completed logical batches are persisted
  per key and NEVER resent on retry; a failed batch is replaced in place
  with the same logical_batch_number and attempt_number + 1; a per-job
  run lock makes one click at most one outbound run; HTTP 429 maps to
  PRODUCT_LOOKUP_RATE_LIMITED with Retry-After honored and no automatic
  retry (legacy pre-checkpoint artifacts trigger one clean reset run).
