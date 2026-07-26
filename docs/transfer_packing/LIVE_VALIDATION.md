# Live validation gates (Build 8)

Controlled, explicitly approved validation of the real API Gateway. Nothing
in this document runs automatically: automated tests are offline-only, both
live gates default to **disabled**, and every live command additionally
requires `--yes` on the command line.

## Safety model

| Layer | Control |
|-------|---------|
| Environment gate | `PILOT_ENABLE_LIVE_AUTH` / `PILOT_ENABLE_LIVE_PRODUCT_LOOKUP` — both default `false` |
| Explicit confirmation | the CLI refuses without `--yes` |
| Request budget | auth = exactly one login; product = ONE batch of 1–3 identifiers; optional Qty comparison = one more request for the same identifier |
| Redaction | probes report status/duration/codes/field names only — never passwords, tokens, `Authorization` headers, or raw credential-bearing bodies |
| No persistence | probe tokens are cleared from memory afterwards; nothing token-bearing is written to disk; raw responses are not persisted |

## 1. Auth probe (one login)

Prerequisites: `API_GATEWAY_BASE_URL`, `API_GATEWAY_USER_ID`,
`API_GATEWAY_PASSWORD` set in the server environment (never in Git);
`PILOT_ENABLE_LIVE_AUTH=true` for the duration of the probe only.

```bash
python -m apps.web.transfer.pilot auth-check --yes
```

Expected output (JSON, redacted): `success`, `duration_seconds`,
`gateway_code`, `access_token_present`, `refresh_token_present`,
`expires_in_seconds`, `error_code`. Re-set the gate to `false` afterwards.

## 2. Product probe (one batch, 1–3 approved identifiers)

Prerequisites: successful auth probe;
`PILOT_ENABLE_LIVE_PRODUCT_LOOKUP=true`; an **approved** location code,
PriceDate, and identifier list agreed with the business beforehand.

```bash
python -m apps.web.transfer.pilot product-check --yes \
  --location <APPROVED-LOCATION> --price-date <YYYY-MM-DD> \
  --plu <APPROVED-IDENTIFIER> --qty 1
```

Default output is a **schema observation**: top-level and record field
names, JSON value types, null/empty/omission counts, record count, and
request/record correlation — no values. Add `--show-values` only when the
business needs to confirm actual Analysis Code / Composition / price
values; even then token- or credential-like keys are stripped.

What the observation must answer before the pilot:

1. exact wire names for Analysis Code 01–15 and Composition #1–4 (casing,
   flat vs nested);
2. whether blank fields are omitted, `null`, or empty strings;
3. whether `plu`/`ean` echo the request and preserve leading zeros;
4. whether unknown identifiers are silently omitted (expected);
5. whether (location, plu) remains a safe correlation key.

The normalization adapter is updated **only** from this evidence, with
sanitized fake-data fixtures added to the offline tests. Raw live
responses are never committed.

## 3. Qty comparison (optional, max two requests total)

Same approved identifier, once with `--qty 1` and once with the approved
comparison value; diff the safe output fields (prices, availability).
Outcome must be recorded in `FUNCTIONAL_SPEC.md` as one of:

- **A** — Qty is lookup-only; `Qty = 1` stays correct;
- **B** — Qty affects pricing; `resolve_lookup_qty()` switches to source
  quantity (code + tests + docs updated together);
- **C** — ambiguous; configuration retained and pilot approval blocked.

## Current status

**Not executed.** Both gates are disabled and no live call has been made.
Analysis Code / Composition wire names and the Qty policy remain
**unconfirmed** until the probes above are approved and run.
