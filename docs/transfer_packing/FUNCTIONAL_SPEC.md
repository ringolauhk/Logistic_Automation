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
