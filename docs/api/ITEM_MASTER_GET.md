# `POST /corpTool/itemMaster-get` — reference (documentation only)

> **Status: NOT used by this project.** The Transfer workflow's product
> enrichment continues to use `POST /corpTool/pluLabel-get` exclusively
> (live-validated in Build 9). This page documents `itemMaster-get` as a
> possible future data source only; nothing in the codebase calls it.
>
> Source: `docs/api/imaginex-api-swagger-v1.json` (the tracked official
> imxapig OpenAPI 3.0 snapshot, operator-provided). Everything below is
> derived from that file — no live call was made to produce this page.

## Operation

| | |
|---|---|
| Path | `POST /corpTool/itemMaster-get` |
| Operation id | `comimaginexapigCorpToolEpGetItemMaster` |
| Tag | Corptool |
| Auth | `JWTBearerAuth` (Bearer JWT from `POST /auth/login`, refresh via `POST /auth/refresh`) |
| Responses | `200` success envelope, `401` Unauthorized |

## Request — `ComimaginexapigCorpToolItemMasterListReqDto`

```json
{
  "requestList": [
    {
      "orgId": "string",
      "locationCode": "string | null",
      "plu": "string"
    }
  ]
}
```

Notes (from the schema): `orgId` and `plu` are required-shaped strings;
`locationCode` is nullable. Unlike `pluLabel-get`'s request items
(`locationCode`/`plu`/`item`/`color`/`size`/`qty`/`priceDate`), there is
no `qty` and no `priceDate` here, but `orgId` is part of each item.

## Response — `ItemMasterResDto_DocHttp200`

Standard envelope (`ComimaginexapigBaseBaseResDto`): `status` (string),
`code` (int32), `reason`, `note`, and `data` as an **array of
`ItemMasterResDto`** records with these fields (all camelCase, flat):

**Identity / classification**
`orgId`, `itemCode`, `itemDesc`, `longItemDesc`, `colorCode`,
`colorDesc`, `colorOrder` (int32), `sizeCode`, `plu`, `ean` (string —
leading zeros safe), `brand`, `brandName`, `season`, `seasonDesc`,
`cat`, `catDesc`, `subcat`, `subcatDesc`, `collection`,
`collectionDesc`, `gender`, `prodLine`, `prodLineDesc`, `model`,
`material`, `supplierItemCode`, `countryOfOrigin`

**Analysis Codes** — `analysisCode01` … `analysisCode15` (nullable
strings; same field names as `pluLabel-get`)

**Compositions** — `compositon1` … `compositon4` (nullable strings;
the API's actual spelling — the second "i" of "composition" is missing
on the wire, exactly as in `pluLabel-get`; never "correct" it)

**Pricing** — `currency`, `originalPrice` (decimal), `currentPrice`
(decimal)

## Differences vs `pluLabel-get` worth knowing

- Extra descriptive fields not returned by `pluLabel-get`:
  `seasonDesc`, `cat`/`catDesc`, `subcatDesc`, `collection`/
  `collectionDesc`, `prodLineDesc`, `model`, `material`,
  `countryOfOrigin`, `colorOrder`.
- Price field names differ: `originalPrice`/`currentPrice` here vs
  `originalRetailPrice`/`discountPrice` on `pluLabel-get`.
- No `locationCode` echo, no `xf_group5/12/16`, no `qty` echo in the
  response schema.
- No `priceDate` in the request — price semantics relative to date are
  therefore undocumented here; `pluLabel-get` remains the source for
  date-effective pricing.

## If it is ever adopted (future build, business approval required)

It must be additive alongside `pluLabel-get`, go through the Build 4
auth client, inherit the Build 9 protections (checkpointing, run lock,
rate-limit handling, redaction), and extend
`tests/test_api_spec_sync.py` with its contract before any live call.
