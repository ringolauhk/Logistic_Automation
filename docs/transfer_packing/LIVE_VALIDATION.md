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

## Current status (Build 9 — executed under explicit approval)

All gates were enabled per-command only and are disabled again. Total live
requests: 3 logins (one per probe/pilot run — probe tokens are never
reused) + 3 product batches (probe Qty=1, probe Qty=2, pilot lookup).
One additional auth attempt was interrupted by a local process timeout
before any result was captured; its downstream request status could not
be proven, and it is recorded here for completeness.

**Confirmed by evidence** (one product, `CMSHKG11`, PriceDate
`2026-07-01`, plus a 2-EAN pilot lookup at `ZZOHK101`, `2026-06-06`):

- Flat camelCase records; no nesting; `data` is a list.
- **Analysis Codes**: `analysisCode01`…`analysisCode15` — always all 15
  present, blanks are empty strings (never null/omitted).
- **Compositions**: `compositon1`…`compositon4` — the wire misspells
  "composition" (missing the second "i") and does not zero-pad; always
  all 4 present, blank-as-empty-string. The Build 5 adapter was extended
  to accept the real spelling (plus the previously tolerated variants).
- **Correlation**: `locationCode` and `plu` echo the request verbatim;
  `(locationCode, plu)` is a safe correlation key; ordering is not relied
  upon. `ean` is a string with leading zeros preserved. `qty` echoes as
  an int. Prices are JSON numbers.
- **Qty policy A confirmed**: Qty=1 vs Qty=2 for the same product
  returned byte-identical business fields except the qty echo —
  `resolve_lookup_qty()` stays 1 and Qty stays out of the deduplication
  key. Evidence covers one approved product, location, and date.
- `xf_group5/12/16` values overlapped with some analysisCode values on
  the sampled product but are **not** a proven mapping — treated as
  independent fields.
- **Unknown-identifier omission**: NOT live-tested (not approved);
  remains specification-based.

Customer Style/Color mappings remain **unconfirmed** and blank — no
accepted evidence form (written confirmation, spec, production code, or
explicit instruction) exists yet.

**Official API spec (post-Build-10 update):** the operator supplied the
authoritative imxapig OpenAPI document, now tracked at
`docs/api/imaginex-api-swagger-v1.json`. It corroborates every live observation
above (envelope shape, `analysisCode01..15`, the misspelled
`compositon1..4`, string `plu`/`ean`, decimal prices, int `qty`, login
`client`/`userId`/`password`/`locale`, refresh `rt`).
`tests/test_api_spec_sync.py` fails loudly if a future spec update drifts
from what the clients expect. When a newer spec is issued, replace that
file and re-run the suite.
