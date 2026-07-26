# Pilot checklist (Build 8)

A controlled end-to-end run of the Transfer Packing workflow with a small
set of **approved** real Transfer Delivery Notes. Do not start before every
Preparation item is checked, and never widen the sample without approval.

## Preparation

- [ ] Working tree clean; the deployed commit recorded (`git rev-parse HEAD`).
- [ ] Backups of any prior `web-data/` content the business wants kept.
- [ ] `docker compose build invoice-extractor-web` reproduces the image.
- [ ] `docker compose config` is valid; no secrets in `compose.yaml`.
- [ ] Server `.env` (never committed) carries the API Gateway credentials;
      `chmod 600 .env`.
- [ ] `python -m apps.web.transfer.pilot doctor` exits 0 or 1 with only
      understood warnings; OCR reports `installed`/`ready`.
- [ ] Live validation completed per `LIVE_VALIDATION.md`: auth probe green,
      product probe schema confirmed, Qty policy decided (A or B — not C).
- [ ] Customer mapping fields configured **only** if the business confirmed
      them; otherwise left blank (columns stay empty by design).
- [ ] `TRANSFER_WORKFLOW_ENABLED=true`; both `PILOT_ENABLE_LIVE_*` gates
      reset to `false` (the workflow itself does not need them).
- [ ] Rollback plan read (`PILOT_ROLLBACK.md`).

## Execution (1–2 approved documents)

- [ ] Start the app; health check `http://<host>:8501/_stcore/health` = ok.
- [ ] Pilot readiness expander shows no blockers and no secret values.
- [ ] Upload the approved Transfer Delivery Note PDFs in carton order.
- [ ] Extraction totals match the printed totals on the source documents.
- [ ] Review screen: correct/exclude as needed; approve.
- [ ] Product lookup runs; enrichment issues reviewed and understood.
- [ ] Packing groups: destinations, carton renumbering, and consolidation
      match the source documents.
- [ ] Generate workbooks; validation status `valid` on every workbook.
- [ ] Download each workbook and the ZIP.
- [ ] Open each workbook in Microsoft Excel — **no repair prompt**.
- [ ] Quantities, cartons, and units match the source documents.
- [ ] Original carton numbers traceable (Carton Mapping sheet).
- [ ] Customer columns: populated per configured mapping, or blank.
- [ ] Refresh the browser and restart the container mid-review once:
      state recovers, nothing duplicates.
- [ ] Search downloads + logs for credentials/tokens: none present.
- [ ] `python -m apps.web.transfer.pilot cleanup --dry-run` output sane.
- [ ] Record the run: `python -c` manifest via the pilot module or note the
      job id; keep `pilot/result.json` (redacted) with the job folder.

## Sign-off

- [ ] Business confirms the workbooks are usable for the warehouse.
- [ ] Defects (if any) logged with job id + safe issue codes — source PDFs
      and workbooks stay out of Git.
- [ ] Decision recorded: proceed, iterate, or roll back.
