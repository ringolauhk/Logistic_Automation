# Pilot rollback plan (Build 8)

If the pilot must be stopped — defects, data concerns, or suspected
credential exposure — follow these steps in order. `main` has never carried
the Transfer workflow, so rollback is always available.

1. **Stop the app**: `docker compose down` (or stop the native
   `streamlit run`). In-flight jobs stay on disk; nothing is lost.
2. **`main` stays untouched** — it still points at the last stable
   invoice-only release; no merge has happened.
3. **Deploy the previous stable image/commit**:
   `git checkout main && docker compose build invoice-extractor-web &&
   docker compose up invoice-extractor-web`. Alternatively keep the branch
   checkout and only flip the feature flag (next step).
4. **Disable the Transfer workflow** without redeploying:
   set `TRANSFER_WORKFLOW_ENABLED=false` (or remove it) in the server
   `.env`, restart the container. The invoice workflow is unaffected.
5. **Preserve pilot evidence**: do NOT delete
   `web-data/transfer-jobs/` — job folders (sources, artifacts,
   `pilot/result.json`) are the audit trail. Copy them to a restricted
   location if the host is being rebuilt.
6. **Credential hygiene**: if any exposure of `API_GATEWAY_PASSWORD` or a
   token is suspected (logs, screenshots, commits), rotate the gateway
   account credentials immediately and set both `PILOT_ENABLE_LIVE_*`
   gates to `false`. Tokens are process-memory only, so a container
   restart invalidates any cached token.
7. **Restore prior configuration**: reinstate the pre-pilot `.env` /
   `compose.yaml` from the backup taken during preparation.
8. **Validate the invoice workflow after rollback**: upload a known-good
   invoice PDF and complete one extraction; check
   `http://<host>:8501/_stcore/health`.
9. **Retain logs** (already secret-free by design) alongside the job
   folders for the post-mortem.
10. **Never auto-delete source evidence**: `pilot cleanup` touches only
    generated outputs/tmp/archived metadata — leave source PDFs governed
    by the documented job retention policy and the business decision.
