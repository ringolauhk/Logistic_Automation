# Transfer Note Packing List — functional specification

A second, independent workflow in the pilot web app: convert uploaded
**Transfer Delivery Note** PDFs into per-destination Excel packing lists.
It never shares job state, domain models, or session state with the
invoice-extraction workflow.

## Purpose

Users upload Transfer Delivery Note PDFs in carton-processing order. The
system (in later builds) extracts product/carton rows, groups them by
**To Loc.** (destination), enriches items via the internal `pluLabel-get`
product API, reassigns carton numbers per destination, and generates one
Excel packing list per destination.

## Build 1 scope (implemented)

- Feature flag `TRANSFER_WORKFLOW_ENABLED` (default **false**; when off the
  invoice UI is unchanged and no selector is shown).
- Workflow selector at the top of the web app; **Invoice Extraction stays
  the default**.
- Transfer Note upload screen: multi-PDF uploader, ordered selection table
  (sequence 1, 2, 3, …), per-file size/page-count/validation status,
  clear-selection, and **Create Transfer Packing Job**.
- Deterministic validation (no OCR/AI) with stable machine codes:
  `NO_FILES`, `UNSUPPORTED_FILE_TYPE`, `EMPTY_FILE`, `FILE_TOO_LARGE`,
  `TOO_MANY_FILES`, `TOO_MANY_PAGES`, `INVALID_PDF`, `DUPLICATE_FILE`
  (duplicates detected by SHA-256 content checksum).
- Job persistence under a dedicated root with explicit upload order,
  original filenames retained for display/audit, atomic metadata writes,
  status `READY_FOR_EXTRACTION`, and refresh recovery.

**Not in Build 1** (explicitly): OCR/extraction, API-Gateway or
`pluLabel-get` calls, access-token handling, carton resequencing, Excel
generation, retention cleanup for transfer jobs, drag-and-drop reordering.

## Build 2 scope (implemented)

Deterministic extraction of the uploaded Transfer Delivery Notes - no LLM,
no cloud calls, no internal API:

- **Page classification per page** (never per PDF): usable embedded text ->
  `embedded_text`; otherwise **local OCR** (`ocr`); failures -> `unreadable`
  with a per-page issue. One bad page never discards the others.
- **OCR fallback**: optional local engine (RapidOCR/onnxruntime - install
  `requirements-ocr.txt` or the `transfer-ocr` extra). Pages are rendered in
  memory only; no image is ever written to disk. Without the dependency,
  scanned pages get `OCR_UNAVAILABLE` issues and text-native extraction
  still works. Single-letter cells (Size S/M) that OCR page detection
  misses are rescued by a recognition-only pass on the exact cell crop.
- **Recognition**: weighted marker scoring (`TRANSFER DELIVERY NOTE`,
  `IMAGINEX`, `D/N#`, `To Loc.`, `Pick Ref`, `EAN Code`, `Carton`,
  `Batch`); OCR may miss markers - no single one is required. Unrecognized
  PDFs are marked `UNRECOGNIZED_DOCUMENT`, never silently accepted.
- **Header parsing** (per page, label-anchored geometry): Batch, From,
  To Loc., Pick Ref, Carton, D/N#, Date, Page. Location values split into
  UPPERCASED code + name with source casing preserved
  (`ZZOHK101 Multi Brand(Outlet)-...` -> `ZZOHK101` + name). Dates
  normalize day-first (`DD/MM/YYYY`) to ISO with the raw string kept.
  Destinations are NEVER inferred from filenames; a page missing To Loc.
  inherits only a document-unique destination (`destination_inherited`
  recorded); conflicts raise `AMBIGUOUS_DESTINATION`.
- **Carton parsing**: carton identity is the PRINTED carton number (leading
  zeros preserved, never invented, never resequenced); a carton may span
  consecutive pages; upload order then page order is retained everywhere.
- **Item lines**: Seq/Item/EAN/Description/Retail Price/Color/Size/Quantity
  with raw + normalized values per field. EAN stays a string (leading
  zeros); quantities (`1 PCS`, `35 UNIT`) normalize to positive integers;
  prices to decimals; malformed rows are retained with issues, never
  dropped; rows keep source order; no merging/deduplication in this build.
- **Total validation** (exact, quantities never adjusted): per-carton
  `Carton Total` and per-document `Grand Total` compared against calculated
  sums; missing printed totals are not errors; unreadable ones warn.
- **Persistence**: `extraction/result.json` (schema_version 1) written
  atomically inside the job directory; browser refresh recovers it; retry
  replaces it atomically (never duplicates); one failed document never
  erases another's results.
- **Job states**: `READY_FOR_EXTRACTION -> EXTRACTING -> EXTRACTED |
  EXTRACTED_WITH_ISSUES | FAILED` (validated transitions; a job stranded in
  `EXTRACTING` by a restart can retry safely). Extraction runs
  synchronously in the page - the pilot architecture - and is refresh-safe
  after completion.

### Issue codes (Build 2)

`UNRECOGNIZED_DOCUMENT`, `UNREADABLE_PAGE`, `OCR_UNAVAILABLE`,
`MISSING_DESTINATION`, `AMBIGUOUS_DESTINATION`, `MISSING_DELIVERY_NOTE_NO`,
`MISSING_CARTON_NO`, `NO_ITEM_LINES`, `MISSING_ITEM_IDENTIFIER`,
`INVALID_EAN`, `MISSING_COLOR`, `MISSING_SIZE`, `INVALID_QUANTITY`,
`INVALID_RETAIL_PRICE`, `MALFORMED_ITEM_ROW`, `CARTON_TOTAL_MISMATCH`,
`DOCUMENT_TOTAL_MISMATCH`, `PRINTED_TOTAL_UNREADABLE`,
`DOCUMENT_EXTRACTION_FAILED`. Severity is `error` (blocks clean acceptance)
or `warning` (kept for review).

### Manual sample validation procedure

With a real Transfer Delivery Note PDF available locally (never committed):
install `requirements-ocr.txt`, enable the workflow, upload the PDF, create
the job, press **Extract Transfer Notes**, and compare the extraction
summary against the printed document: per-carton `Carton Total`, the
`Grand Total`, carton numbers, D/N#, and To Loc. The reference sample
(13 scanned pages, 12 cartons, 277 units) extracts with every carton total
and the grand total matching exactly.

**Not in Build 2** (explicitly): access-token retrieval, API-Gateway
authentication, `pluLabel-get`, product enrichment, line consolidation,
carton reassignment, delivery-invoice-number generation, Excel packing-list
generation, editable correction UI (Build 3+).

## Build 3 scope (implemented): review, correction, approval

A controlled review screen over the Build 2 extraction. **The extraction
artifact is immutable**; review data is a separate artifact:

```
<job>/review/review.json            # schema_version 1
<job>/review/review-stale-*.json    # archived stale reviews (audit)
```

### Original / corrected / effective value model

Every reviewed entity (document header, carton, line) stores a frozen
`original` snapshot plus an explicit `corrections` map:

| corrections state | meaning | effective value |
|---|---|---|
| field absent | unchanged | original |
| `field: "value"` | corrected (trimmed; codes uppercased) | corrected value |
| `field: null` | **deliberately cleared** | empty |

Raw extracted values are never overwritten. In the editors, an EMPTY cell
never clears a value - the literal token `<clear>` does. Correcting a field
back to its original removes the correction. Every real change appends an
audit entry (entity, field, original, previous corrected, new corrected,
UTC timestamp); repeated identical saves add nothing.

### Exclusion

Documents, cartons, and lines can be excluded **only with a reason**.
Exclusion cascades at evaluation time (document -> its cartons -> their
lines), so re-including a parent restores children automatically. Excluded
records keep their originals and corrections and stay listed.

### Issue resolution (deterministic)

Resolution is a pure function of effective values + exclusions - clicking
Save never resolves anything. Blocking (Build 2 `error`) vs warning
(`warning`) severity is kept. Key rules: structural document problems
(`UNRECOGNIZED_DOCUMENT`, `UNREADABLE_PAGE`, `OCR_UNAVAILABLE`,
`DOCUMENT_EXTRACTION_FAILED`) resolve only by excluding the document;
`MISSING_DESTINATION`/`MISSING_DELIVERY_NOTE_NO`/`MISSING_CARTON_NO`
resolve when the effective value exists; `MISSING_ITEM_IDENTIFIER` /
`INVALID_EAN` / `MALFORMED_ITEM_ROW` resolve when the line becomes
lookup-ready or is excluded; `INVALID_QUANTITY` only by a positive integer
or exclusion; `CARTON_TOTAL_MISMATCH` / `DOCUMENT_TOTAL_MISMATCH`
recalculate from effective INCLUDED quantities against the printed totals
(unreadable printed totals downgrade to warnings). **Documented rule:** a
valid EAN alone makes a line lookup-ready (EAN is the primary identifier);
unresolved `MISSING_COLOR`/`MISSING_SIZE` remain visible warnings and never
block approval. The fallback identifier is Item + Color + Size.

### Lookup readiness and approval

A line is lookup-ready when its effective EAN is valid (digits only after
trimming, 8-14 digits, leading zeros preserved) OR effective Item, Color
and Size are all present. **Approve for Product Lookup** requires: at least
one included document/carton/line; every included document has a
destination code and D/N#; every included carton has a carton number and
destination; every included line has a positive integer quantity and is
lookup-ready; zero unresolved blocking issues; the saved review matches the
current extraction checksum. Approval sets review status `APPROVED` and job
status `READY_FOR_PRODUCT_LOOKUP`. **No API is called.**

### Stale-review protection

`review.json` records the SHA-256 checksum of `extraction/result.json`.
Re-running extraction changes the checksum; the saved review is then marked
`STALE`, can never be approved, and rebuilding archives it as
`review-stale-<UTC>.json` - corrections are never silently reused against
changed source data and never silently discarded.

### Job states (Build 3)

`EXTRACTED | EXTRACTED_WITH_ISSUES -> REVIEW_IN_PROGRESS ->
READY_FOR_PRODUCT_LOOKUP | REVIEW_REJECTED`;
`READY_FOR_PRODUCT_LOOKUP -> REVIEW_IN_PROGRESS` (reopen before enrichment
begins); review states may return to `EXTRACTING` (re-extract), which
triggers the staleness protection. All transitions validated.

### Audit limitation (single-user pilot)

There is no login/user identity; every review records
`reviewed_by: "local-user"` and UTC timestamps. Adding real identity would
require the authentication phase that is explicitly out of pilot scope.

**Not in Build 3** (explicitly): access-token retrieval, authentication
refresh (`/auth/login`, `/auth/refresh`), `pluLabel-get`, product
enrichment, Analysis Code / Composition values, carton resequencing, line
consolidation, delivery-invoice numbering, Excel output.

## Build 4 scope (implemented): API Gateway authentication client

A reusable, backend-only authentication layer for the future product
lookup. **No product endpoint is called in Build 4.**

### Confirmed contract (ImagineX API Gateway spec v0.851-CorpTools +
### working label-print integration)

- `POST {base}/auth/login` with
  `{"client": "<id>", "userId": "<user>", "password": "<pass>",
  "locale": "en-US"}`; `POST {base}/auth/refresh` with `{"rt": "<refresh>"}`.
- Envelope: `{status, code, reason (fallback message/msg), note, data}`.
  **Success = HTTP 2xx AND `code == 100000`** (HTTP 200 alone is not
  success). Failed login example: `code = 100001`.
- Token data: `data.accessToken` (JWT), `data.refreshToken`,
  `data.expire_in` (access-token lifetime in seconds; number or numeric
  string). Refresh may rotate the refresh token near its end of life; a
  stored refresh token is **never overwritten by an empty/null**
  replacement (spec rule). Expired refresh token -> HTTP 401 -> re-login.

### Architecture (`apps/web/transfer/gateway_auth.py`)

`ApiGatewayAuthConfig` + env loader; `ApiGatewayCredentials`
(repr-suppressed); `ApiGatewayTokenSet` (obtained_at/expires_at with
configurable expiry skew; no expiry info -> valid until invalidated);
`TokenCache` - **process-local, thread-safe, in-memory only** (each app
process/container maintains its own cache; nothing is persisted to disk,
Streamlit session state, job JSON, review JSON, or the browser);
`AuthTransport` protocol with an httpx production implementation and fake
transports in tests; `ApiGatewayAuthClient` with `login()`, `refresh()`,
`ensure_access_token()` (cached -> refresh -> login; a definitively
rejected refresh clears stale state and falls back to exactly ONE fresh
login; concurrent callers serialize on the cache lock and reuse the first
result), `invalidate_access_token()`, `clear_tokens()`, and the Build 5
hooks `get_authorization_header()` / `handle_unauthorized()` (invalidate +
re-acquire; Build 5 will retry a failed product batch once).

### Retry policy

Only transport timeouts/failures and HTTP 5xx are retried, capped by
`API_GATEWAY_MAX_AUTH_RETRIES` with short injected backoff. HTTP 4xx and
gateway-level rejections are never retried. No recursion, no loops.

### Typed errors + redaction

Stable error codes: `AUTH_CONFIGURATION_ERROR`, `AUTH_TRANSPORT_ERROR`,
`AUTH_TIMEOUT`, `AUTH_RESPONSE_INVALID`, `AUTH_GATEWAY_REJECTED`,
`AUTH_LOGIN_FAILED`, `AUTH_REFRESH_FAILED`, `AUTH_TOKEN_MISSING`,
`AUTH_TOKEN_EXPIRED`, `AUTH_ACCESS_DENIED`, `AUTH_RETRY_EXHAUSTED` - each
carrying operation, HTTP status, gateway code, and a retryable flag.
Central `redact()` removes values under sensitive keys (case-insensitive:
password, access_token/accessToken, refresh_token/refreshToken,
authorization, token, secret, and the refresh body key `rt`). Errors and
logs never contain credentials or token values, even partially.

### Readiness

`readiness()` validates configuration WITHOUT any network call and powers
the UI status on `READY_FOR_PRODUCT_LOOKUP` jobs: Configured /
Not configured / Configuration error (variable names only - never values).
Job states are unchanged; authentication stays internal until Build 5.

**Not in Build 4** (explicitly): `pluLabel-get`, EAN or Item+Color+Size
lookup, product enrichment, Analysis Code 01-15, Composition #1-4,
source/API comparison, line consolidation, carton resequencing,
delivery-invoice numbering, packing-list Excel output.

## Build 5 scope (implemented): product lookup and enrichment

Enriches every included reviewed line via `POST {base}/corpTool/pluLabel-get`
using the Build 4 auth client. **Stops before grouping, renumbering,
consolidation, and Excel.**

### Confirmed product API contract (spec v0.851-CorpTools §3.3 + label-print)

- Request: `{"RequestList": [{"LocationCode", "PLU", "PriceDate"
  (YYYY-MM-DD), "Qty" (int)}]}` with `Content-Type: application/json` and
  `Authorization: Bearer <accessToken>`. Multiple entries allowed,
  including the same PLU at different locations; no documented batch
  maximum (configurable, default 50).
- Response: `{status, code, reason, note, data: [records]}`; success =
  HTTP 2xx AND `code == 100000`. **The gateway silently OMITS non-existing
  PLU-location combinations from `data` - absence IS the not-found
  signal**, so partial batch results are normal and correlation uses the
  echoed `(locationCode, plu)` (a requested EAN may also match the
  record's authoritative `ean`), never array position. Uncorrelatable
  records raise `PRODUCT_LOOKUP_RESPONSE_AMBIGUOUS`.
- Record wire fields (camelCase; PascalCase tolerated): orgId,
  locationCode, brand, brandName, currency, itemCode, colorCode,
  colorDesc, sizeCode, plu, ean, itemDesc, longItemDesc, subcat, gender,
  prodLine, supplierItemCode, xf_group5/12/16, originalRetailPrice,
  discountPrice, qty (echo). Prices arrive as JSON numbers and are stored
  as strings. **Analysis Code 01-15 / Composition #1-4 wire names appear in
  NO local specification**; the normalizer captures any key matching
  `analysisCode01`- / `composition01`-style patterns into
  `analysis_code_01..15` / `composition_01..04`, keeps all `xf_group*`
  fields, and stores the full token-free raw record - so live responses
  are captured losslessly whatever the real names are (confirm on first
  live call).

### Identifier rules

EAN first (string, leading zeros kept, 8-14 digits); fallback is the
LITERAL concatenation `Item + Color + Size` (uppercased, no separator; a
repeated color suffix inside the item code is never removed - the spec's
own PLU `CM0010007804M5C2WAHM` is exactly itemCode+colorCode+sizeCode).
Lines with neither identifier are never sent to the API and get
`PRODUCT_LOOKUP_IDENTIFIER_MISSING`. Fallback runs only after a definitive
not-found on the EAN stage - never after auth failures, malformed
responses, or unexhausted transient errors - and is itself deduplicated;
at most two identifier attempts per unique key.

### Planning, deduplication, batching

Input is ONLY the approved, checksum-current review (stale/unapproved/
malformed data is refused; effective corrected values are used, excluded
entities are skipped). Identical `(LocationCode, PriceDate, PLU)` requests
are deduplicated with every source line mapped back to the shared result -
source rows are NEVER merged and quantities never change. Deterministic
order (upload -> page -> line; first-seen sequence recorded). Batches of
`PRODUCT_LOOKUP_BATCH_SIZE` (default 50); only failed batches are retried.

### Policies (isolated; confirm before live use)

`resolve_lookup_location()` = effective destination To Loc. code (the
destination's price list). `resolve_price_date()` = effective delivery-note
date, else the explicit `PRODUCT_LOOKUP_PRICE_DATE` override, else a
BLOCKING planning problem - today's date is never silently used.
`resolve_lookup_qty()` = constant 1 (lookup-only): label-print sends line
quantities and the spec example hints Qty may influence returned prices -
**open business decision documented here**; a constant keeps deduplication,
correlation, and unit-price comparison exact.

### Authentication integration

`ProductGatewayClient` composes the Build 4 client: `ensure_access_token()`
per batch (cached token reused), Authorization header built internally, on
HTTP 401 `handle_unauthorized()` (refresh -> one re-login) then ONE retry
of that batch; a second 401 fails the run as
`PRODUCT_LOOKUP_ACCESS_DENIED`. Transient timeouts/transport failures/5xx
retry up to `PRODUCT_LOOKUP_MAX_RETRIES`; 4xx and gateway rejections are
never retried. Tokens never appear in logs, the UI, or artifacts (tested).

### Comparison and issue severity

Source (reviewed effective) and API values are stored side by side; API
values NEVER overwrite review data. Blocking: identifier missing, auth
failure, access denied, uncorrelatable response, not found, multiple
matches, item/color/size mismatch, and EAN mismatch on an EAN-keyed match.
Warnings: description wording, retail-price difference, EAN difference
when the CONSTRUCTED fallback matched (the API EAN is authoritative), and
source gaps the API resolves.

### Persistence and states

`product_lookup/result.json` (schema_version 1, atomic) with the review
checksum, config summary (no secrets), batches, lookups, normalized
products, per-line enrichments, issues, and summary. Editing/re-approving
the review changes the review checksum -> enrichment is marked STALE (and
archived as `result-stale-*.json` on the next run); a stale enrichment can
never feed packing-list generation. Job states:
`READY_FOR_PRODUCT_LOOKUP -> PRODUCT_LOOKUP_IN_PROGRESS ->
PRODUCT_LOOKUP_COMPLETE | PRODUCT_LOOKUP_WITH_ISSUES |
PRODUCT_LOOKUP_FAILED`, with validated retries and re-approval routes.

**Not in Build 5** (explicitly): destination workbook grouping, carton
resequencing, same-carton line consolidation, delivery-invoice numbering,
customer attribute mapping (which Analysis Code means lc_style etc. is a
later build), Excel generation, ZIP output.

## Build 6 scope (implemented): packing preparation

Deterministic, fully local transformation of the enriched data into one
destination package per future workbook. **No API call, no Excel, no ZIP.**
Artifact: `packing/result.json` (schema_version 1, atomic); upstream
artifacts are never modified.

### Input boundary

Preparation requires: readable extraction; APPROVED review whose checksum
matches the extraction; a current (non-stale, well-formed, non-failed)
product enrichment; a preparable job state; and at least one eligible line.
Stale/malformed/unapproved inputs are refused - there is no fallback to raw
extraction values. **Blocking policy:** line-scoped blocking product issues
(not found, multiple matches, identity mismatch) make those lines
ineligible (`PACKING_LINE_BLOCKED_BY_PRODUCT_ISSUE`) and mark their
destination `PACKING_DESTINATION_BLOCKED` (result `WITH_ISSUES`); warnings
never block; nothing disappears silently.

### Grouping, carton identity, ordering

Groups form on the effective reviewed `To Loc.` code (trimmed, uppercased,
never inferred from filenames); the destination name is the reviewed
effective name. Destination order = first appearance in upload -> page ->
line order (not alphabetical). Source-carton identity is the Build 3 carton
entity (unique per document) plus a persisted source key (upload sequence,
file, D/N, original number, first page) - original carton `001` reused
across files stays distinct. Cartons order by (upload sequence, first
source page, first line, original number as final tie-break); a carton
spanning pages stays one carton.

### Resequencing and consolidation

Generated numbers restart at `PACKING_CARTON_START` (default 1) per
destination, zero-padded to `PACKING_CARTON_PAD_WIDTH` (default 3, so
`001`), growing past 999 (1000, 1001) without wrapping; the original
number is stored beside the generated one and never overwritten.
Identical lines combine ONLY within one generated carton, on the
authoritative API identity (item + color + size + EAN/PLU, destination in
the key for safety): quantities sum, contributing reviewed line IDs and
full source references are retained in first-seen order. Never merged:
across cartons, across destinations, by description alone, or when API
identity fields are incomplete (`PACKING_PRODUCT_IDENTITY_INCOMPLETE`
warning keeps the line separate); identical identity resolving to two
different product records raises `PACKING_CONSOLIDATION_CONFLICT`.

### Delivery invoice numbers

One per destination: `<PACKING_INVOICE_PREFIX>-<DEST>-<YYYYMMDD>-<SEQ>`
(default prefix `PL`; SEQ = first-appearance destination sequence, 3
digits). Generated server-side at first successful preparation and carried
forward VERBATIM on unchanged reruns and refreshes (checksum-matched
against the prior artifact). **Pilot limitation: uniqueness is per job
only** - there is no global counter. Suggested future filename:
`Packing_List_<Destination>_<InvoiceNo>.xlsx` (name only; no file is
created).

### Staleness and states

`packing/result.json` stores the extraction, review, and product-lookup
checksums; any upstream change marks it stale, and the next run archives
it as `result-stale-<ts>.json`. Job states:
`PRODUCT_LOOKUP_COMPLETE | PRODUCT_LOOKUP_WITH_ISSUES ->
PACKING_PREPARATION_IN_PROGRESS -> PACKING_PREPARATION_COMPLETE |
PACKING_PREPARATION_WITH_ISSUES | PACKING_PREPARATION_FAILED`, with
validated retries (including stranded IN_PROGRESS) and explicit rerun from
COMPLETE until workbook generation exists.

**Not in Build 6** (explicitly): final workbook generation, Excel
formatting/styling, customer Analysis Code mapping, ZIP output, printing,
production deployment changes.

## Upload order is a business rule

Cartons follow **user upload order, then PDF page order**. The uploader's
order is captured as an explicit persistent `sequence` (1-based) on each
file and is never re-derived by sorting filenames. Stored filenames are
sequence-prefixed (`001-<sanitized>.pdf`) so on-disk order matches too.
The model reserves room for manual reordering in a later build.

## Job model

`transfer_job.json` (atomic write; `schema_version: 1`):

| Field | Meaning |
|---|---|
| `job_id` | `tjob-<UTCstamp>-<hex12>` — distinct shape from invoice `job-…` ids |
| `job_type` | always `transfer_packing` (invoice jobs are implicitly `invoice_extraction`) |
| `created_at` / `status` | UTC ISO time; `READY_FOR_EXTRACTION` \| `CANCELLED` \| `FAILED` |
| `summary` | file count, total pages, total bytes |
| `files[]` | ordered: `sequence`, `original_name`, `stored_name`, `size_bytes`, `mime`, `sha256`, `page_count`, `status` (`UPLOADED`/`VALIDATED`/`INVALID`), `messages` |
| `extraction`, `outputs` | reserved empty extension points for later builds |

Directory layout:

```
web-data/transfer-jobs/
  tjob-YYYYMMDDTHHMMSS-xxxxxxxxxxxx/
    transfer_job.json
    input/
      001-first_note.pdf
      002-second_note.pdf
```

Isolation guarantees: separate root (`TRANSFER_JOBS_DIR`), distinct id
regex in both loaders, and a `job_type` check on load — the invoice
recovery/cleanup code never sees transfer jobs, and vice versa.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TRANSFER_WORKFLOW_ENABLED` | `false` | show the workflow selector + transfer page |
| `TRANSFER_MAX_FILES` | `50` | max PDFs per job |
| `TRANSFER_MAX_FILE_MB` | `50` | max size per PDF |
| `TRANSFER_MAX_PAGES` | `500` | max combined pages per job |
| `TRANSFER_JOBS_DIR` | `./web-data/transfer-jobs` | transfer job storage root |

Run with the feature enabled:

```bash
TRANSFER_WORKFLOW_ENABLED=true streamlit run apps/web/app.py
# Docker: add TRANSFER_WORKFLOW_ENABLED=true to .env used by the web service
```

## Confirmed rules for later builds (NOT implemented in Build 1)

- Grouping is based on **To Loc.**
- Cartons follow user upload order and PDF page order.
- Carton numbering restarts from **001 per destination**.
- **EAN** is the primary `pluLabel-get` lookup identifier; the fallback
  identifier is **Item + Color + Size**.
- An **access token** must be obtained before product API calls.
- Identical item/color/size rows combine **only within the same carton**;
  identical products remain separate across cartons.
- One **Excel workbook** per destination; one **delivery invoice number**
  per destination.
- The API returns **Analysis Code 01–15** and **Composition #1–4**.
- Customer mappings will be configurable.

## Current limitations

- No extraction/API/Excel yet: creating a job stores the PDFs and stops at
  `READY_FOR_EXTRACTION`.
- Transfer jobs are not auto-deleted (invoice retention does not apply to
  them); remove `web-data/transfer-jobs/` manually if needed.
- No reordering after upload (planned).
- Uploaded transfer PDFs are never downloadable through the UI.
