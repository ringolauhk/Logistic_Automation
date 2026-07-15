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
