# Invoice Extractor (PoC)

Batch-extracts structured data from invoice PDFs — any vendor, any layout, no
per-vendor templates — and writes an Excel workbook.

## How it works

1. **Classify** each PDF per page via direct text extraction (PyMuPDF). Pages
   averaging > 20 alphanumeric chars are "text-native"; otherwise "image-only"
   (scanned).
2. **Text-native path** — the raw extracted text goes to a cheap text-only
   Gemini Flash call that normalizes it into a fixed JSON schema. This path
   never touches a vision API. (If Gemini fails entirely, it falls back to a
   *text-only* Claude call.)
3. **Image-only path** — pages are rendered to ~200 DPI PNGs and sent to
   Gemini vision. If the response errors, times out, or fails schema
   validation, the same images are retried on Claude (Sonnet) vision.
4. **Validation** — flags `needs_review` when required fields are null or
   `sum(line_items.amount) + tax_amount` isn't reasonably close to
   `total_amount`.
5. **Output** — `results.xlsx` with three sheets: `Invoices` (one row per
   invoice), `LineItems` (foreign-keyed by `invoice_id`), `NeedsReview`
   (flagged subset with reasons). Per-file logging goes to `output/run.log`.

If **both** providers fail for a file, a null row is still emitted with
`needs_review=true` — the batch never crashes.

## Setup

Requires Python 3.11+.

```bash
cd "Logistic automation"
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### API keys

```bash
cp .env.example .env
```

Edit `.env` and set:

- `GEMINI_API_KEY` — from https://aistudio.google.com/apikey
- `ANTHROPIC_API_KEY` — from https://platform.claude.com/

Keys are read from `.env` (or the environment) — never hardcoded. Optional
overrides (`GEMINI_MODEL`, `CLAUDE_MODEL`, `RENDER_DPI`, etc.) are documented
in `.env.example`.

## Usage

Drop sample invoice PDFs into `samples/`, then:

```bash
# Full pipeline
python -m invoice_extractor run --input ./samples --output ./output/results.xlsx

# Smoke test (same pipeline + printed summary: X processed, Y needs_review, Z errors)
python test_pipeline.py
```

### Testing stage by stage (no API tokens spent)

```bash
# Stage 1-2: text extraction + text/image classification per file
python -m invoice_extractor classify --input ./samples

# Stage 3 (image path input): render pages to PNGs so you can eyeball
# exactly what the vision API would receive
python -m invoice_extractor render --input ./samples --output ./output/pages
```

Once `classify` routes files the way you expect, run the full `run` command
to exercise the LLM stages.

## Output

- `output/results.xlsx` — `Invoices` / `LineItems` / `NeedsReview` sheets
- `output/run.log` — per-file log: classification, provider used, retries,
  fallbacks, timing, errors

Each invoice row records `extraction_method` (`text` | `gemini_vision` |
`claude_vision`), and the log records which provider actually produced the
final result.

## Scope

Single-user CLI PoC only — no web UI, auth, or multi-tenancy (explicitly a
later phase).
