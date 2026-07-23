# Pilot web UI

A **single-user** browser UI for the invoice extractor: upload PDFs, start a
run, watch safe progress, download the results. It is a limited pilot tool,
not a production platform.

> **No login.** Anyone who can reach the port can upload invoices and trigger
> **paid provider calls**. Keep it on a trusted LAN, localhost, or Tailscale.
> Public internet exposure is **unsupported**.

> **Privacy.** During extraction, uploaded invoice content (text and rendered
> page images) is sent to the configured external model provider. Uploads and
> outputs are stored **temporarily** on the machine running the app (per-job
> folders, deleted after the retention window or via the in-app delete
> button). There is no permanent storage — download your results. Do not
> upload unrelated confidential files. Debug artifacts are off by default.

## Starting and stopping

Docker (recommended):

```bash
docker compose build invoice-extractor-web
docker compose up invoice-extractor-web        # http://localhost:8501
# stop with Ctrl+C or: docker compose down
```

Native (developers):

```bash
pip install -r requirements-web.txt            # streamlit + pins
streamlit run apps/web/app.py                  # binds localhost:8501
```

The CLI and its Docker image are unaffected — Streamlit is installed only in
the separate `invoice-extractor-web` image / `requirements-web.txt`.

## Using the page

1. **Upload invoices** — PDFs only, drag-and-drop, multiple files. Limits
   (env-configurable): `WEB_MAX_FILES=25`, `WEB_MAX_FILE_MB=25`,
   `WEB_MAX_TOTAL_MB=200`. Files are validated (extension, `%PDF` signature,
   size, duplicates) and nothing is silently truncated.
2. **Validate & prepare** — files are stored in a fresh job folder and
   classified locally (free, no provider calls). You'll see per-file page
   counts and a **conservative upper-bound provider-attempt estimate** with
   its assumptions. Actual requests are usually lower — successful models,
   validation, and budgets stop escalation. No dollar estimate is shown (the
   app has no reliable pricing data).
3. **Run settings** — optional downloadable run log and run-metadata JSON
   (both **off** by default). Advanced expander: `MAX_TEXT_PAGES`,
   `MAX_VISION_PAGES` (dense scans: use 1–2), attempt/cost caps, timeout.
   These apply to this run's worker only. API keys are never shown or edited
   in the browser.
4. **Start extraction** — preflight checks (provider config present, no other
   job running), then a **worker subprocess** runs the same engine as the
   CLI. Only one extraction can run at a time; a second tab or double-click
   shows "Another extraction is currently running".
5. **Progress** — live file/chunk/attempt/model updates from structured
   events (metadata only, never invoice content). A browser refresh
   rediscovers the running job — it never starts a second one.
6. **Cancel** — sends the worker the same signal as Ctrl+C on the CLI: no new
   provider calls, completed files are kept, a valid partial workbook/usage
   CSV is written, and the page shows "Cancelled by operator". The Cancel
   button only ever signals a verified worker process (never a reused PID).
7. **Results** — summary counts (files, review, failures, requests, repairs,
   escalations, reported cost, elapsed) and a compact NeedsReview table with
   safe categories. Extracted invoice values are only in the workbook.
8. **Downloads** — `results.xlsx`, `results.usage.csv` (OpenRouter runs),
   `results.run.json` and `run.log` when enabled. Fixed allowlist: nothing
   else — including the uploaded source PDFs — is downloadable.

## Job storage and retention

Each run gets its own folder under `WEB_JOBS_DIR` (`/data/jobs` in Docker →
`./web-data` on the host): `input/` (uploads), `output/` (artifacts),
`logs/`, `status.json`, `events.jsonl`. Jobs — including abandoned prepared
ones — are deleted after `WEB_JOB_RETENTION_HOURS=24` (cleanup runs at app
start and before each new job), or immediately via **Delete job files now**.
The active job is never deleted.

## Remote pilot access

The compose default publishes host port `8501:8501` on all interfaces so
pilot users on the **trusted LAN** can reach the app directly
(`http://<host>:8501`) - anyone on that network can upload and spend, so keep
the network trusted. For remote (off-LAN) pilots prefer an **authenticated
private proxy**:

```bash
# Tailscale Serve: private, authenticated, no port changes needed
tailscale serve 8501
```

Localhost-only alternative (single-machine use): change the compose mapping
to `"127.0.0.1:8501:8501"`. Whatever the binding, do **not** expose the port
to the public internet; that is unsupported (no login, no rate limiting, no
HTTPS termination). Native `streamlit run` (without Docker) still binds
localhost only.

## Transfer Note Packing List (feature-flagged)

A second, independent workflow — upload Transfer Delivery Note PDFs in
carton order, create a Transfer Packing job, and (Build 2) run local
deterministic extraction: embedded text first, optional local OCR for
scanned pages (`pip install -r requirements-ocr.txt` — no cloud calls),
carton/item parsing with exact printed-total validation, and (Build 3) a
review screen: correct or exclude headers/cartons/lines with full audit of
original vs corrected values, deterministic issue resolution, and approval
to `READY_FOR_PRODUCT_LOOKUP`. Product enrichment and per-destination
packing lists come in later builds.
Hidden unless `TRANSFER_WORKFLOW_ENABLED=true`; the invoice workflow stays
the default and is unchanged. Transfer jobs are stored separately under
`web-data/transfer-jobs/` and are not auto-deleted in Build 1. Full details:
`docs/transfer_packing/FUNCTIONAL_SPEC.md`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Another extraction is currently running" but nothing is | The stale lock is reclaimed automatically once the old worker PID is gone (or its heartbeat expires). Wait a few seconds and reload. |
| Start button disabled | Fix the red preflight message (missing key/model config) in the server's `.env`, then reload. |
| Output files owned by another user (Linux) | Run compose with `HOST_UID=$(id -u) HOST_GID=$(id -g)`. |
| Job vanished | Retention (`WEB_JOB_RETENTION_HOURS`) removed it; download results promptly. |
| Port already in use | Stop the other process or change the host-side port in `compose.yaml`. |

No secrets, uploaded PDFs, or job data belong in Git: `web-data/` is
git-ignored and excluded from Docker build contexts.
