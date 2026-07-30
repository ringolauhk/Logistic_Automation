# `POST /corpTool/itemMaster-get` — reference and Transfer usage

> **Status: the ACTIVE product-lookup endpoint of the Transfer workflow
> (Build 11).** It replaced `POST /corpTool/pluLabel-get` for Transfer
> enrichment. Authentication is unchanged (`POST /auth/login` with the
> shared account from `.env`); the **organization is selected by the user
> in the Transfer UI** and its Organization ID is sent as `orgId` — never
> derived from the login account or token.
>
> Implemented request form (per the contract resolution below):
> `{"requestList": [{"orgId": "<selected ID>", "plu": "<EAN or
> item+color+size>"}]}` — `locationCode` is **omitted** because it is
> nullable in the schema and the workflow has no reliable
> organization-compatible location. Batching uses the `requestList`
> array (batch size 50). Responses are correlated by the echoed
> `plu`/`ean` with an `orgId` echo check (`PRODUCT_ORG_MISMATCH` blocks
> wrong-organization records); `originalPrice`/`currentPrice` map to the
> normalized original/discount price slots.
>
> Source: `docs/api/imaginex-api-swagger-v1.json` (the tracked official
> imxapig OpenAPI 3.0 snapshot, operator-provided). Everything below is
> derived from that file — no live call was made to produce this page.
> This live form is **pending controlled live validation**.

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

## Adoption status (Build 11)

Adopted for the Transfer workflow through the Build 4 auth client with
every Build 9 protection intact (organization-aware checkpoint/resume,
completed-batch skipping, attempt tracking, run lock, 429 handling with
`Retry-After`, legacy full-rerun confirmation, redaction). Checkpoints
record the Organization ID; results from one organization are never
resumed for another, and pre-Build-11 artifacts without organization
identity require the explicit confirmed restart.
`tests/test_api_spec_sync.py::TestItemMasterContract` pins this contract;
controlled live validation of the new endpoint is still pending.
