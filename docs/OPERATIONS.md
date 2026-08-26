# Operations guide

Operator-facing guide for running the invoice extractor day to day. Assumes
the package is already installed in a virtual environment.

> **Privacy first.** Never commit real invoice PDFs, real ground truth, `.env`,
> outputs, usage CSVs, or debug artifacts. All of those are git-ignored by
> default. Logs, review reasons, usage CSVs, run metadata, and benchmark
> reports are built to contain **no** prompts, page text, base64/image data,
> provider response bodies, or API keys.

## 1. Installation assumptions

- Python 3.11+.
- Dependencies installed into a virtual environment (`.venv`).
- API keys supplied via `.env` (copy from `.env.example`) or the environment.

## 2. Activate the virtual environment

```bash
source .venv/bin/activate      # macOS/Linux
```

Verify readiness without spending anything:

```bash
python -m invoice_extractor doctor
```

`doctor` is offline by default: it reports Python/packages, path checks, the
gateway, **masked** key presence (values never printed), configured model
lists, chunk sizes, retries/timeout, safety-limit status, the overwrite
policy, and whether debug artifacts are enabled. It makes **zero** provider
calls. (`doctor --live` sends a tiny generated probe to the *direct* Gemini/
Claude models only — never an invoice, never OpenRouter.)

## 3. Input / output folders

- Put PDFs in an input folder (e.g. `./samples`).
- Choose an output workbook path with `--output` (default `./output/results.xlsx`).
- The `.usage.csv` sidecar and optional run-metadata JSON are written beside it.

## 4. Configure models

Two gateways, selected by `LLM_GATEWAY`:

- `direct` (default): Gemini primary, Claude fallback. Needs `GEMINI_API_KEY`.
- `openrouter`: ordered text/vision model ladders. Needs `OPENROUTER_API_KEY`,
  `OPENROUTER_TEXT_MODELS`, and (for image pages) `OPENROUTER_VISION_MODELS`.

Model lists are ordered and comma-separated; the first accepted result wins,
later entries are escalation tiers. A stray/empty entry (`a,,b`, `,a`, `a,`)
is **rejected**, not silently dropped.

## 5. Recommended safety limits (OpenRouter)

Set these before real batches (all optional; unset = no limit, which triggers
a one-time warning):

| Variable | What it caps | Suggested start |
|----------|--------------|-----------------|
| `MAX_MODEL_ATTEMPTS_PER_FILE` | model attempts across all chunks/routes of one PDF | `3` |
| `MAX_COST_USD_PER_FILE` | reported cost per PDF | `0.05` |
| `MAX_COST_USD_PER_RUN` | reported cost for the whole batch | `1.00` |
| `MAX_TEXT_PAGES` | text pages per request | `2` |
| `MAX_VISION_PAGES` | image pages per request | `5` (see chunk guidance) |
| `MAX_RETRIES` | transport retries per request | `3` |
| `REQUEST_TIMEOUT_SECONDS` | per-request timeout | `120` |

## 6. Classify (no API calls)

```bash
python -m invoice_extractor classify --input ./samples
```

Shows per-page text/image/blank classification so you know which routes a
batch will use before spending anything.

## 7. Run extraction

```bash
python -m invoice_extractor run --input ./samples --output ./output/results.xlsx
```

Useful flags:

- `--overwrite` — replace existing outputs (see §11).
- `--log-file PATH` — write a persistent run log (console logging is always on;
  there is **no** automatic `./output/run.log`).
- `--run-metadata PATH` — write a small, privacy-safe run-metadata JSON
  (run id, timestamps, status, and per-file runtime/method/provider/model/
  needs_review/error/completed/request_count/cost — no invoice content).

## 8. Understanding progress logs

Before every paid request you'll see a safe line such as:

```
INFO [run] inv.pdf: text chunk 2/3 pages 3-4 - starting primary model 1/3 requested=vendor/text-a timeout=120s
```

Attempt type is `primary`, `repair`, or `escalation`. A slow request is
visibly in flight rather than looking frozen. Progress lines never contain
prompts, invoice text, image bytes, base64, keys, or responses. Each file ends
with a `done ... requests=N repair=N escalation=N` summary, and the run ends
with total requests/cost/elapsed and the output paths.

## 9. Outputs

- **Workbook** (`results.xlsx`) — exactly three sheets: `Invoices`,
  `LineItems`, `NeedsReview`.
- **Usage CSV** (`results.usage.csv`) — one row per OpenRouter request
  (metadata only). Written only under the OpenRouter gateway.
- **Run metadata** (optional) — only when `--run-metadata` is passed.

All outputs are written **atomically**: each is written to a temporary file in
the destination directory and only then renamed into place. Every temp is
written successfully **before** any existing final is replaced, so a failure
mid-write leaves all existing outputs untouched. (Replacing multiple files is
not one transaction — if the process is killed *between* the final renames some
files may be new and some old — but the temp-first ordering keeps that window
tiny.)

## 10. Handling NeedsReview

A `needs_review` row is **not** a program failure — the batch still exits 0.
Reasons are compact and safe (missing required fields, header/total conflicts,
totals inconclusive, partial extraction, budget reached, unreadable PDF, ...).
Open the `NeedsReview` sheet, fix or confirm by hand, and move on.

### Automatic text → vision fallback (bounded)

A **text-native** page can still hide a required field inside an image — the
confirmed case is a seller name that exists only in a letterhead logo, so
every text model answers correctly and is still rejected for missing
`seller_name`.

When (and only when) **every** attempt of the text ladder was rejected for
that one reason, the run re-reads those pages **once** through the normal
vision path:

- **Trigger** — all recorded text attempts have rejection category
  `missing_required_fields` (a structured signal, not text matching), the
  document has no image pages of its own, and the failed pages fit in one
  vision chunk.
- **Hard bounds** — at most **one** fallback per document, **one** chunk of
  at most `MAX_VISION_PAGES` pages; the run-wide and per-file cost/attempt
  budgets are checked first and are never bypassed; the fallback can never
  trigger itself again.
- **Never triggers for** provider/transport errors, auth failures, timeouts,
  rate limiting, malformed envelopes or JSON, budget exhaustion, operator
  cancellation, documents already routed to vision, or missing/invalid
  `OPENROUTER_VISION_MODELS` (which skips the fallback with no call).
- **Provenance** — the result records that the fallback ran, which pages, the
  fields that triggered it, and whether it recovered them; the extra request
  appears once in the usage CSV (`route=vision`) and in the request counts.
- **Nothing is invented.** If the source is genuinely ambiguous (e.g. prices
  written only as `$` with no currency code anywhere), the field stays empty
  and the row remains in review. Documents such as zero-value Sales Orders
  may therefore still need a human decision — that is the correct outcome.

Failure labels distinguish the two situations exactly: `provider_failure`
means no usable provider response was received; `missing_required_fields`
means providers answered but the document never supplied the fields.

### Product-first extraction (any product document)

The scanner accepts product documents generally - invoices, commercial
invoices, packing lists, delivery notes, free-goods support lists - and the
LINE ITEMS are the payload. Validation is two-level:

- **Hard (fails the document):** no usable product row. A row is usable when
  it identifies a product at all - item code, barcode, or a non-empty
  description. Rows that came back but identify nothing are a corrupt
  extraction and fail; a document with no rows at all keeps its existing
  "no line items extracted" review clause so header data is still exported.
- **Soft (warns, never fails):** every document-level field (seller, invoice
  date/number, buyer, currency, subtotal, tax, total, payment terms) and most
  per-row fields (quantity, unit price, amount, barcode, code, description).
  Missing values stay blank - never invented - are listed in the workbook's
  `missing_fields` column and highlighted pale yellow.

Only the four formerly-required fields (invoice date, currency, seller name,
total) route a document to review when absent; the rest are reported without
dragging every ordinary invoice into the review queue. A missing header never
costs another provider call: the ladder does not escalate, and the bounded
vision fallback now triggers only when the product rows themselves are
missing or unusable.

`document_type` is set only from explicit wording in the source (e.g. a
"Free Goods Support Item List" title); an unrecognized document is processed
identically with a null type. A document declaring itself free of charge
keeps its printed prices and total exactly as printed - neither zeroed nor
promoted to a payable amount - and is flagged so a reviewer can decide.

Semantic safety is unchanged: a MISSING value warns, a CONTRADICTORY value
(systematic column shift) still rejects the model attempt.

### Line items must be semantically possible

A model answer can be structurally perfect and still wrong: PDF text layers
sometimes scramble the item table's column order, and a model that maps by
stream position can return each row's TOTAL PRICE as the quantity (with
amount recomputed as that total x unit price). Every attempt is therefore
checked before acceptance:

- **Rejected as `line_item_semantic_mismatch`** when a SYSTEMATIC shift
  pattern appears - two or more rows whose quantity x unit price exceeds
  the stated invoice total while the line sum overshoots it several-fold,
  or two or more rows whose quantity literally equals the printed line
  amount while the arithmetic disagrees. Thresholds are relationships to
  the document's own totals - never absolute magnitudes, so legitimate
  wholesale quantities always pass.
- **Never rejected** for a single inconsistent row (could be a discount or
  bundle - left to the aggregate totals reconciliation), for credit /
  free-of-charge / negative lines, or for rounding within the shared
  tolerance.
- A rejected attempt escalates through the existing model ladder; if every
  text model fails validation the bounded one-attempt vision fallback may
  run under its usual conditions. `MAX_MODEL_ATTEMPTS_PER_FILE` is always
  honored, and the rejection is never `provider_failure` - the provider
  responded.
- Source values are never rewritten to make the arithmetic pass.

Line items additionally carry an optional `barcode` field: tables that
print both a PRODUCT CODE and an EAN/UPC barcode keep both (`item_code`
and `barcode` respectively). Invoices without barcodes are unaffected.

### Currency must be evidenced by the source

A currency is kept only when the document itself shows it:

- **Accepted** — an explicit ISO code (`HKD`, `USD`, `EUR`, `GBP`, …) or a
  qualified symbol (`HK$`, `US$`, `S$`, `NT$`, …), or a symbol that belongs
  to exactly one currency (`€`, `£`, `₹`, …). Matching is case-insensitive
  and never fires inside a longer word (`USDA` is not `USD`).
- **Rejected** — a bare `$` or `¥`. These are shared by many currencies, so
  they evidence none of them. Addresses, seller/buyer country, locale and
  model world-knowledge are **not** evidence: a Hong Kong address plus `$`
  does not make it HKD.

When a model returns a currency the source does not evidence, the value is
dropped (kept as provenance), the row is routed to review with
`currency lacks explicit source evidence`, and every other extracted field —
including a seller name recovered by the vision fallback — is preserved.
The check is deterministic and offline: it never triggers another provider
or vision attempt. Documents whose pages expose no text at all (pure scans)
are exempt, since there is nothing to verify against.

## 11. Rerunning with --overwrite

By default a run **refuses** (before any provider call) if the workbook, its
`.usage.csv`, or the run-metadata JSON already exists, so you never silently
clobber a prior result:

```
FATAL: output already exists (...); no provider calls were made. Re-run with
--overwrite to replace, or choose a different --output.
```

Pass `--overwrite` to replace them (atomically, only after a successful run).
`benchmark score` has the same `--overwrite` contract for its report outputs.

## 12. Stopping safely with Ctrl+C

Press Ctrl+C to stop. The tool:

- stops issuing new provider calls and retries immediately;
- prints **no** traceback;
- writes a valid **partial** workbook + usage CSV if at least one file
  completed (nothing if zero completed);
- records the in-flight file as a controlled interrupted review row;
- exits with code **130**.

## 13. Common errors

See `docs/TROUBLESHOOTING.md` for the full catalogue (missing keys/models,
timeouts, rate limits, payment required, collisions, unwritable output,
corrupt PDFs, interruption).

## 14. Cost control (paid-call formula)

Worst-case application-issued HTTP calls for one file:

```
(text_chunks x text_models + vision_chunks x vision_models) x 2 x MAX_RETRIES
```

(the `x 2` is one primary + at most one repair per model), **capped** by
`MAX_MODEL_ATTEMPTS_PER_FILE` across the whole file when set.

Examples (`MAX_RETRIES=3`):

- **Simple 1-page text invoice**, 1 text model → `1x1 x 2 x 3 = 6` max.
- **6-page text invoice**, `MAX_TEXT_PAGES=2` → 3 chunks, 3 models →
  `3x3 x 2 x 3 = 54` uncapped; `MAX_MODEL_ATTEMPTS_PER_FILE=3` → `3 x 2 x 3 = 18`.
- **Multi-page scan**, 6 image pages, `MAX_VISION_PAGES=5` → 2 chunks, 2 vision
  models → `2x2 x 2 x 3 = 24` uncapped; cap at 3 → `3 x 2 x 3 = 18`.

`MAX_COST_USD_PER_FILE` / `MAX_COST_USD_PER_RUN` stop further calls once the
reported cost crosses the limit. Costs come only from what the provider
reports; unknown costs stay unknown and are surfaced, never fabricated.

## 15. Chunk-size guidance (dense scans)

The default `MAX_VISION_PAGES=5` is fine for typical documents but a **dense
13-page scanned invoice truncated** with five image pages in one request.
Larger chunks mean fewer (cheaper) requests but higher truncation risk. For a
scanned-heavy pilot, start `MAX_VISION_PAGES=1` or `2`. Remember the per-file
attempt cap must account for the resulting chunk count.

## 16. Privacy precautions

- Keep `.env` out of git (already ignored).
- Never point `--output`/`--log-file`/`--run-metadata` at a tracked path.
- Keep `SAVE_DEBUG_ARTIFACTS=false` in shared environments — when enabled it
  persists failed provider responses, which may contain full invoice contents.

## 17. Benchmark scoring

Score an already-produced workbook against human-authored ground truth,
entirely offline:

```bash
python -m invoice_extractor benchmark score \
  --manifest ./benchmark/manifest.json \
  --workbook ./output/results.xlsx \
  --usage ./output/results.usage.csv \
  --output ./output/benchmark_report.xlsx
```

Optional `--thresholds` makes the command exit non-zero on a failed threshold.
See `benchmark/examples/` for a synthetic manifest, ground truth, and
thresholds template. Real-data benchmark directories are git-ignored.

## 18. No real PDFs or secrets in Git

Real invoice PDFs, `.env`, `output/`, `*.usage.csv`, and real benchmark
ground-truth/manifests/reports are git-ignored. Only synthetic fixtures and
templates are committed. Double-check `git status` before committing.

## 19. Pilot web UI

A single-user browser front-end to the same engine (uploads, progress,
cancellation, downloads) runs as the separate `invoice-extractor-web` compose
service on `http://localhost:8501`. Same providers, budgets, outputs, and
privacy rules as the CLI; one extraction at a time; job files are temporary
(24 h retention). See `docs/WEB_UI.md`.
