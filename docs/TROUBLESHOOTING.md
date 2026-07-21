# Troubleshooting

Every message below is safe to share: none contain provider response bodies,
prompts, invoice content, or API keys. Where a problem is document-specific,
the message names the safe file / route / page range and the knob to adjust.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Batch completed (even if some invoices are `needs_review`); or no PDFs found |
| `1`  | Config/log/output error, output collision without `--overwrite`, unwritable output, batch could not complete, or output could not be written |
| `2`  | `--input` does not exist (extraction); invalid benchmark manifest/ground truth (`benchmark score`) |
| `130`| Interrupted by the operator (Ctrl+C) |

`needs_review` rows are **not** failures — they never change the exit code by
themselves.

## Configuration

| Symptom | Cause | Fix |
|---------|-------|-----|
| `OPENROUTER_API_KEY is not set` (review row) | OpenRouter gateway without a key | Set `OPENROUTER_API_KEY` in `.env` |
| `OPENROUTER_TEXT_MODELS ...` in a review row | No text ladder configured | Set `OPENROUTER_TEXT_MODELS=vendor/model,...` |
| `OPENROUTER_VISION_MODELS ...` in a review row | Image pages but no vision ladder | Set `OPENROUTER_VISION_MODELS=...` (image pages need it) |
| `... contains an empty entry (a stray leading, double, or trailing comma)` | `a,,b`, `,a`, or `a,` | Remove the stray comma; empty entries are rejected, not dropped |
| `... contains a malformed model id` | Not `vendor/model` shaped | Use the `vendor/model` form |
| `MAX_COST_USD_PER_FILE must be non-negative` (and similar) | Bad numeric limit | Use a non-negative number |
| `no OpenRouter safety limits configured ...` (warning) | No attempt/cost caps | Optional but recommended — see OPERATIONS.md §5 |

## Provider conditions (safe categories only)

These appear as compact review reasons; raw provider bodies are never shown.

| Category | Meaning | What to adjust |
|----------|---------|----------------|
| `truncated` | Response hit the output-token cap | Lower `MAX_TEXT_PAGES` / `MAX_VISION_PAGES` (esp. dense scans) |
| `rate_limited` (HTTP 429) | Provider throttling | Retry later; reduce concurrency of separate runs |
| `payment_required` (HTTP 402) | Billing/credit issue | Check your OpenRouter balance |
| `model_unavailable` (HTTP 404) | Model id not available to your key | Fix the id; confirm on OpenRouter |
| `timeout` | Request exceeded the timeout | Raise `REQUEST_TIMEOUT_SECONDS`; lower `RENDER_DPI`/chunk size |
| `... failed on all configured models` | Every model in the ladder failed for that chunk | Inspect the named page range; add/adjust models or limits |

Under `LLM_GATEWAY=openrouter` the message says **"all configured models"**
(only the ladder was tried) — not "all providers".

## Output and files

| Symptom | Cause | Fix |
|---------|-------|-----|
| `output already exists (...); no provider calls were made` | Collision without `--overwrite` | Add `--overwrite`, or choose a new `--output` |
| `--output ... is a directory, not a file` | `--output` points at a folder | Give a file path ending in `.xlsx` |
| `output location ... is not writable` | Parent dir not creatable/writable | Choose a writable location |
| `outputs could not be written (...); any existing outputs were left unchanged` | A write failed mid-run | Existing outputs are intact; fix the cause and re-run |
| `unreadable PDF: ...` (review row) | Corrupt/encrypted/zero-page PDF | Re-export the PDF; encrypted files are reported, not cracked |
| `No PDFs found ... - nothing to do` | Input folder has no `.pdf` files | Point `--input` at the right folder |

## Interruption (Ctrl+C)

- Exit code `130`, no traceback.
- If ≥1 file completed: a valid **partial** workbook + usage CSV are written;
  the in-flight file is recorded as an interrupted review row.
- If 0 files completed: nothing is written (`Interrupted before any file
  completed - no output written`).
- New provider calls and retries stop immediately.

## Benchmark

| Symptom | Cause | Fix |
|---------|-------|-----|
| `BENCHMARK CONFIG ERROR: ...` (exit 2) | Bad manifest/ground truth (duplicate id, missing GT file, unsupported field, bad decimal/date, `tax`+`tax_amount`) | Fix the named case/file/field |
| `report output already exists (...)` (exit 1) | Report collision | Add `--overwrite` or change `--output` |
| threshold `... FAIL` (exit 1) | A supplied threshold was not met | Investigate the metric, or adjust the threshold |

## Logs

- Console logging is always on. There is **no** automatic `./output/run.log`.
- Pass `--log-file PATH` for a persistent log.
- Controlled errors never print a Python traceback; a traceback indicates an
  unexpected internal fault worth reporting.

## Pilot web UI

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Another extraction is currently running" with no job visible | A previous worker died; its lock is reclaimed automatically once the PID is gone or the heartbeat expires (~2 min) | Wait briefly and reload the page |
| Start button shows a configuration error | Missing provider key/model in the server's `.env` | Fix `.env` where the web service runs, restart it |
| Cancel shows "Could not verify the worker process" | Fail-closed identity check: the recorded PID no longer matches a live worker | The job ends on its own; reload — no signal was sent to an unrelated process |
| Job or downloads disappeared | Retention cleanup (`WEB_JOB_RETENTION_HOURS`, default 24 h) | Download results promptly after a run |
| Page unreachable from another machine | Firewall, or the mapping was rebound to localhost | The compose default `"8501:8501"` serves the LAN at `http://<host>:8501`; check the host firewall, or use `tailscale serve 8501` for off-LAN access |

More detail: `docs/WEB_UI.md`.
