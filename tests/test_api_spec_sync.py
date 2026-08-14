"""The official imxapig OpenAPI spec (docs/api/imaginex-api-swagger-v1.json,
operator-provided) is the authoritative API reference. These tests keep
the Transfer clients honest against it: when a newer spec is dropped in,
any drift from what the adapters expect fails loudly here. Offline only -
the spec file is data, never a reason to call anything."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "docs" / "api" / "imaginex-api-swagger-v1.json"


def _spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _schema(name):
    return _spec()["components"]["schemas"][name]["properties"]


class TestSpecPresence:
    def test_spec_tracked_and_valid(self):
        spec = _spec()
        assert spec["info"]["title"] == "imxapig"
        for path in ("/auth/login", "/auth/refresh",
                     "/corpTool/pluLabel-get"):
            assert path in spec["paths"], path

    def test_spec_contains_no_secret_values(self):
        # field NAMES like "password" are schema, not secrets; actual
        # values would appear as long opaque strings - the spec has none
        text = SPEC_PATH.read_text(encoding="utf-8")
        for banned in ("eyJ", "Bearer ey", "websrv-password",
                       "API_GATEWAY_PASSWORD="):
            assert banned not in text, banned


class TestAuthContract:
    def test_login_request_fields_match_build4_client(self):
        fields = _schema("ComimaginexapigAuthSvcLoginReqDto")
        # the Build 4 client sends exactly client/userId/password/locale;
        # targetData is optional in the spec and intentionally unused
        for sent in ("client", "userId", "password", "locale"):
            assert sent in fields, sent
        assert fields.get("targetData", {}).get("nullable") is True

    def test_refresh_and_token_fields(self):
        assert "rt" in _schema("ComimaginexapigAuthSvcRefreshReqDto")
        token = _schema("ComimaginexapigAuthSvcUserTokenResDto")
        for field in ("accessToken", "expire_in", "refreshToken"):
            assert field in token, field

    def test_envelope_shape(self):
        base = _schema("ComimaginexapigBaseBaseResDto")
        for field in ("status", "code", "reason", "note", "data"):
            assert field in base, field
        assert base["code"]["type"] == "integer"


class TestPluLabelContract:
    def test_request_fields_cover_what_we_send(self):
        # NOTE: the spec documents camelCase request properties; the wire
        # format the client sends (LocationCode/PLU/PriceDate/Qty,
        # confirmed working live in Build 9) binds case-insensitively on
        # the ASP.NET server. Do not change the proven wire casing.
        fields = _schema("ComimaginexapigCorpToolLabelReqDto")
        for field in ("locationCode", "plu", "qty", "priceDate"):
            assert field in fields, field
        wrapper = _schema("ComimaginexapigCorpToolLabelListReqDto")
        assert "requestList" in wrapper

    def test_response_fields_cover_the_normalizer(self):
        """Every wire field the Build 5 normalizer maps must exist in the
        official response DTO - including the misspelled compositon1..4
        and all fifteen analysisCode fields (spec-confirmed, matching the
        Build 9 live observation)."""
        fields = _schema("ComimaginexapigCorpToolLabelResDto")
        expected = ["orgId", "locationCode", "brand", "brandName",
                    "currency", "itemCode", "colorCode", "colorDesc",
                    "sizeCode", "plu", "ean", "itemDesc", "longItemDesc",
                    "subcat", "gender", "prodLine", "supplierItemCode",
                    "xf_group5", "xf_group12", "xf_group16",
                    "originalRetailPrice", "discountPrice", "qty"]
        expected += [f"analysisCode{i:02d}" for i in range(1, 16)]
        expected += [f"compositon{i}" for i in range(1, 5)]
        for field in expected:
            assert field in fields, field
        # identifiers are strings on the wire (leading zeros safe)
        assert fields["ean"]["type"] == "string"
        assert fields["plu"]["type"] == "string"
        # the correctly spelled variant does NOT exist on the wire - the
        # adapter's spelling tolerance is for OUR older fixtures only
        assert "composition1" not in fields
        assert "composition01" not in fields

    def test_normalizer_accepts_every_spec_attribute_field(self):
        from apps.web.transfer.product_lookup import normalize_record
        fields = _schema("ComimaginexapigCorpToolLabelResDto")
        raw = {name: ("1" if meta.get("type") == "integer"
                      else 1.0 if meta.get("type") == "number"
                      else f"V{i}")
               for i, (name, meta) in enumerate(fields.items())}
        product = normalize_record(raw)
        for i in range(1, 16):
            assert product[f"analysis_code_{i:02d}"] is not None, i
        for i in range(1, 5):
            assert product[f"composition_{i:02d}"] is not None, i
        assert product["ean"] and product["plu"]


class TestItemMasterContract:
    """Build 11: the Transfer workflow's ACTIVE endpoint is
    /corpTool/itemMaster-get. These tests pin the tracked contract the
    client is built against."""

    def test_endpoint_and_auth_documented(self):
        spec = _spec()
        operation = spec["paths"]["/corpTool/itemMaster-get"]["post"]
        assert operation["security"] == [{"JWTBearerAuth": []}]

    def test_request_dto_orgid_plu_and_nullable_location(self):
        fields = _schema("ComimaginexapigCorpToolItemMasterReqDto")
        assert fields["orgId"]["type"] == "string"
        assert fields["plu"]["type"] == "string"
        # locationCode is nullable and NOT required -> the client omits it
        assert fields["locationCode"].get("nullable") is True
        wrapper = _schema("ComimaginexapigCorpToolItemMasterListReqDto")
        assert "requestList" in wrapper          # batching supported
        schema = _spec()["components"]["schemas"][
            "ComimaginexapigCorpToolItemMasterReqDto"]
        assert "required" not in schema

    def test_client_request_matches_the_dto(self):
        from apps.web.transfer.product_lookup import ProductLookupKey
        item = ProductLookupKey(org_id="100009", plu="0123",
                                identifier_type="EAN").request_item()
        allowed = set(_schema("ComimaginexapigCorpToolItemMasterReqDto"))
        assert set(item) <= allowed
        assert item == {"orgId": "100009", "plu": "0123"}

    def test_response_fields_cover_the_transfer_mapping(self):
        fields = _schema("ItemMasterResDto")
        expected = ["orgId", "itemCode", "itemDesc", "longItemDesc",
                    "colorCode", "colorDesc", "sizeCode", "plu", "ean",
                    "brand", "brandName", "season", "subcat", "gender",
                    "prodLine", "supplierItemCode", "countryOfOrigin",
                    "currency", "originalPrice", "currentPrice"]
        expected += [f"analysisCode{i:02d}" for i in range(1, 16)]
        expected += [f"compositon{i}" for i in range(1, 5)]
        for field in expected:
            assert field in fields, field
        assert fields["ean"]["type"] == "string"
        assert fields["plu"]["type"] == "string"
        # NOT in this DTO: the old pluLabel names / echoes - the mapper
        # must not rely on them
        for absent in ("locationCode", "originalRetailPrice",
                       "discountPrice", "xf_group5", "qty"):
            assert absent not in fields, absent

    def test_normalizer_maps_itemmaster_prices_and_attributes(self):
        """Synthesize a record from the ItemMasterResDto field list and
        prove the normalizer ingests it - including the explicit
        originalPrice/currentPrice -> original/discount price mapping."""
        from apps.web.transfer.product_lookup import normalize_record
        fields = _schema("ItemMasterResDto")
        raw = {name: ("1" if meta.get("type") == "integer"
                      else 12.5 if meta.get("type") == "number"
                      else f"V{i}")
               for i, (name, meta) in enumerate(fields.items())}
        product = normalize_record(raw)
        for i in range(1, 16):
            assert product[f"analysis_code_{i:02d}"] is not None, i
        for i in range(1, 5):
            assert product[f"composition_{i:02d}"] is not None, i
        assert product["ean"] and product["plu"] and product["org_id"]
        assert product["original_retail_price"] == "12.5"  # originalPrice
        assert product["discount_price"] == "12.5"         # currentPrice
        assert product["location_code"] is None            # not in DTO
        assert product["xf_groups"] == {}                  # not in DTO
