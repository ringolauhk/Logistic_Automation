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

**Build 11 endpoint change — live validation PENDING:** the Transfer
lookup now calls `/corpTool/itemMaster-get` with
`{"requestList": [{"orgId", "plu"}]}` (organization selected in the UI;
`locationCode` omitted per the nullable schema). The Build 9 evidence
above validated `pluLabel-get`; the itemMaster form is implemented
strictly from the tracked spec and offline tests. Before pilot use,
run one controlled probe under the usual double gate:

```bash
PILOT_ENABLE_LIVE_PRODUCT_LOOKUP=true python -m apps.web.transfer.pilot \
  product-check --yes --org <APPROVED-ORGANIZATION-ID> --plu <APPROVED-ID>
```

to confirm: envelope shape, plu/ean echo, orgId echo, AC01-15 and
compositon1-4 population, and originalPrice/currentPrice semantics.

## Build 12 — controlled itemMaster-get probes (executed)

Organization **100009** (IMAGINEX Hong Kong), approved identifiers only,
one request per probe under the standing double gate. No Stage 2 batch,
no UI batch run, and the historical 481-lookup job was never touched.

**Confirmed**

- `/corpTool/itemMaster-get` is reachable and authorized for org 100009:
  a composite PLU (item+color+size form) returned HTTP 200, gateway code
  100000, exactly one correlated record.
- Omitting `locationCode` works - the schema's nullable field can be left
  out entirely, as the client does.
- The record carries the expected shape: `orgId`/`plu` echoes, string
  `ean` with leading zeros, all fifteen `analysisCode01..15` present
  (blanks as empty strings), the misspelled `compositon1..4`, `currency`,
  and `originalPrice`/`currentPrice` as JSON numbers. No `locationCode`,
  no `xf_group*`, no `qty` echo - exactly as the tracked spec documents.
- An approved **EAN** identifier returned HTTP 200 with gateway code
  **400012** and reason/note stating that no price information could be
  retrieved for the given PLUs. Authentication and transport succeeded.
- **400012 is a business-envelope rejection meaning "no match / no price
  data" for the identifier(s) sent** - not an entitlement, endpoint,
  environment or `locationCode` problem. The client therefore records it
  with its HTTP status, gateway code and a bounded, sanitized
  reason/note snippet (Build 12 diagnostics) instead of an opaque error.

**Unresolved - business/API confirmation still required**

1. Whether `itemMaster-get` accepts EAN values in `plu` at all, or only
   composite PLUs. The single EAN tested produced 400012 while the
   composite PLU succeeded; one product is not a general answer.
2. Partial-batch semantics: for a batch where some identifiers match and
   others do not, does the gateway return the found rows with 100000, or
   400012 for the whole batch? This determines whether 400012 should map
   to a per-batch "not found" instead of a run failure.

Until both are answered, the client keeps treating 400012 as a batch
error (safe, no silent data loss) and no broad live batch is run.
