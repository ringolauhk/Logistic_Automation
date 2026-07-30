"""Product lookup + enrichment, Build 5: contract/config, review boundary,
identifier planning, batching, auth integration, request/response handling,
fallback, comparison, persistence, states, and UI wiring. Fully offline -
fake product transports and fake auth clients only."""

import json
from pathlib import Path

import pytest

from apps.web.job_manager import JobError
from apps.web.transfer import extraction
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import models as tm
from apps.web.transfer import product_lookup as pl
from apps.web.transfer import review as rv
from apps.web.transfer.gateway_auth import (
    ApiGatewayAuthConfig,
    AuthError,
    TransportFailure,
    TransportTimeout,
)
from tests.test_transfer_extraction import ROW_A, ROW_B, note_pdf

ROOT = Path(__file__).resolve().parent.parent

# ROW_A: item ZEHE380331E997, ean 0210116339257, color E997, size S, qty 1
# ROW_B: item ZETF381237E085, ean 0210116369698, color E085, size XS, qty 2
EAN_A = "0210116339257"
EAN_B = "0210116369698"
CONSTRUCTED_A = "ZEHE380331E997E997S"        # item+color+size, suffix kept


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    for name in ("PRODUCT_LOOKUP_PATH", "PRODUCT_LOOKUP_BATCH_SIZE",
                 "PRODUCT_LOOKUP_TIMEOUT_SECONDS",
                 "PRODUCT_LOOKUP_MAX_RETRIES", "PRODUCT_LOOKUP_PRICE_DATE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def approved_job(tmp_path, page_specs=None, approve=True):
    pdf = note_pdf(tmp_path / "note-src.pdf", page_specs or [{}])
    uploads = [("note.pdf", Path(pdf).read_bytes())]
    validated, issues = tjobs.validate_transfer_uploads(uploads)
    assert issues == []
    job_id = tjobs.create_transfer_job(uploads, validated)
    extraction.run_extraction(job_id, use_default_adapter=False)
    review = rv.get_or_create_review(job_id)
    rv.save_review(job_id, review)
    if approve:
        rv.approve_review(job_id)
    return job_id


ORG_HK = "100009"                       # IMAGINEX Hong Kong (approved)
ORG_TW = "100012"                       # IMAGINEX Taiwan (approved)


def wire_record(plu, *, org=ORG_HK, ean=None, item=None, color=None,
                size=None, desc="TOP - SQ NK BRA", price=1400.00,
                location=None, **extra):
    """Synthetic ItemMasterResDto-shaped record (spec: no locationCode,
    no xf_group*, no qty echo; prices are originalPrice/currentPrice).
    `location` is accepted for legacy call sites and ignored."""
    record = {
        "orgId": org, "brand": "ZE",
        "brandName": "Test Brand", "currency": "HKD",
        "itemCode": item or "ZEHE380331E997",
        "colorCode": color or "E997", "sizeCode": size or "S",
        "plu": plu, "ean": ean if ean is not None else plu,
        "itemDesc": desc, "longItemDesc": desc, "colorDesc": "BLACK",
        "subcat": "X", "subcatDesc": "Sub", "cat": "C1", "gender": "L",
        "prodLine": "N/A", "season": "FW26", "seasonDesc": "Fall 2026",
        "supplierItemCode": "295900078001", "countryOfOrigin": "CN",
        "originalPrice": price, "currentPrice": 399.00,
    }
    record.update(extra)
    return record


def envelope(records, code=100000):
    return {"status": "successful", "code": code,
            "reason": "Operation/Data Retrieval successful!",
            "note": "Operation successful!", "data": records}


class FakeAuth:
    """Deterministic Build 4 stand-in."""

    def __init__(self, token="tok-A", fail=False):
        self.token = token
        self.fail = fail
        self.ensure_calls = 0
        self.unauthorized_calls = 0
        self.config = ApiGatewayAuthConfig(base_url="https://gw.test/devgapi")

    def ensure_access_token(self):
        self.ensure_calls += 1
        if self.fail:
            raise AuthError("AUTH_LOGIN_FAILED", "rejected",
                            operation="login")
        return self.token

    def handle_unauthorized(self):
        self.unauthorized_calls += 1
        self.token = "tok-B"
        return self.token


class FakeTransport:
    """Scripted product transport; records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, body, *, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers),
                           "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_lookup(job_id, responses, *, auth=None, config=None, **kwargs):
    auth = auth or FakeAuth()
    transport = FakeTransport(responses)
    kwargs.setdefault("org_id", ORG_HK)
    kwargs.setdefault("org_name", "IMAGINEX Hong Kong")
    enrichment = pl.run_product_lookup(job_id, auth=auth,
                                       transport=transport, config=config,
                                       **kwargs)
    return enrichment, transport, auth


# --- contract / config ------------------------------------------------------------

class TestConfig:
    def test_defaults_and_endpoint_path(self):
        config = pl.load_product_config()
        assert config.lookup_path == "/corpTool/itemMaster-get"
        assert config.batch_size == 50
        assert config.timeout_seconds == 120
        assert config.max_retries == 3

    @pytest.mark.parametrize("name,value", [
        ("PRODUCT_LOOKUP_BATCH_SIZE", "0"),
        ("PRODUCT_LOOKUP_BATCH_SIZE", "abc"),
        ("PRODUCT_LOOKUP_TIMEOUT_SECONDS", "0"),
        ("PRODUCT_LOOKUP_MAX_RETRIES", "-1"),
        ("PRODUCT_LOOKUP_PRICE_DATE", "06/06/2026"),
    ])
    def test_invalid_config_rejected(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        assert pl.product_config_problems()
        with pytest.raises(pl.ProductError) as err:
            pl.load_product_config()
        assert err.value.code == pl.PRODUCT_CONFIGURATION_ERROR

    def test_readiness_reports_names_never_values(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_BASE_URL", "https://gw.test")
        monkeypatch.setenv("API_GATEWAY_USER_ID", "real-user-id-x")
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "real-pass-x")
        blob = json.dumps(pl.readiness())
        assert "real-user-id-x" not in blob and "real-pass-x" not in blob


# --- review boundary --------------------------------------------------------------

class TestReviewBoundary:
    def test_approved_current_review_accepted(self, tmp_path):
        job_id = approved_job(tmp_path)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        assert plan.line_count == 2

    def test_unapproved_review_rejected(self, tmp_path):
        job_id = approved_job(tmp_path, approve=False)
        with pytest.raises(JobError):
            pl.build_plan(job_id, org_id=ORG_HK)

    def test_stale_review_rejected(self, tmp_path):
        job_id = approved_job(tmp_path)
        extraction.run_extraction(job_id, use_default_adapter=False)
        with pytest.raises(JobError):
            pl.build_plan(job_id, org_id=ORG_HK)

    def test_excluded_lines_omitted_and_corrections_used(self, tmp_path):
        job_id = approved_job(tmp_path)
        review = rv.load_review(job_id)
        rv.set_exclusion(review, "line", "D001-C001-L002", True, "damaged")
        rv.apply_correction(review, "line", "D001-C001-L001", "ean",
                            "0099900011122")
        rv.save_review(job_id, review)
        rv.approve_review(job_id)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        assert plan.line_count == 1
        assert plan.lookups[0].key.plu == "0099900011122"   # corrected value
        assert all(EAN_A != p.key.plu for p in plan.lookups)  # original unused


# --- identifier planning ----------------------------------------------------------

class TestPlanning:
    def test_ean_primary_with_leading_zeros(self, tmp_path):
        job_id = approved_job(tmp_path)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        assert [p.key.plu for p in plan.lookups] == [EAN_A, EAN_B]
        assert all(p.key.identifier_type == "EAN" for p in plan.lookups)
        assert plan.lookups[0].key.plu.startswith("0")
        assert isinstance(plan.lookups[0].key.plu, str)

    def test_missing_ean_uses_constructed_with_repeated_suffix(self, tmp_path):
        job_id = approved_job(tmp_path)
        review = rv.load_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "ean",
                            rv.CLEAR if hasattr(rv, "CLEAR") else None)
        from apps.web.transfer.review_models import CLEAR
        rv.apply_correction(review, "line", "D001-C001-L001", "ean", CLEAR)
        rv.save_review(job_id, review)
        rv.approve_review(job_id)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        first = plan.lookups[0]
        # item ZEHE380331E997 already ends with color E997 - kept anyway
        assert first.key.plu == CONSTRUCTED_A
        assert first.key.identifier_type == "CONSTRUCTED"

    def test_no_identifier_creates_issue_and_skips_api(self, tmp_path):
        # Approval gates normally prevent identifier-less lines; simulate a
        # post-approval edit (status stays APPROVED) - the planner's
        # defense-in-depth must still skip the API for that line.
        job_id = approved_job(tmp_path)
        review = rv.load_review(job_id)
        from apps.web.transfer.review_models import CLEAR
        for fld in ("ean", "color_code"):
            rv.apply_correction(review, "line", "D001-C001-L001", fld, CLEAR)
        rv.save_review(job_id, review)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        assert plan.no_identifier_lines == 1
        issues = [i for i in plan.line_issues
                  if i["code"] == pl.PRODUCT_LOOKUP_IDENTIFIER_MISSING]
        assert issues and issues[0]["line_id"] == "D001-C001-L001"
        assert all("D001-C001-L001" not in p.line_ids for p in plan.lookups)

    def test_duplicate_ean_deduplicated_lines_kept_separate(self, tmp_path):
        dup_rows = (ROW_A, ("2",) + ROW_A[1:], ROW_B)
        job_id = approved_job(tmp_path, [{"rows": dup_rows,
                                          "carton_total": None}])
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        assert plan.line_count == 3
        assert len(plan.lookups) == 2                   # deduplicated
        first = next(p for p in plan.lookups if p.key.plu == EAN_A)
        assert len(first.line_ids) == 2                 # both source lines
        assert first.first_seen_sequence == 1

    def test_deterministic_order(self, tmp_path):
        job_id = approved_job(tmp_path)
        a = [p.key for p in pl.build_plan(job_id, org_id=ORG_HK).lookups]
        b = [p.key for p in pl.build_plan(job_id, org_id=ORG_HK).lookups]
        assert a == b

    def test_org_from_ui_selection_is_the_key_identity(self, tmp_path):
        """Build 11: keys carry the UI-selected Organization ID; no
        location and no price date exist in the lookup identity."""
        job_id = approved_job(tmp_path)
        plan = pl.build_plan(job_id, org_id=ORG_HK)
        key = plan.lookups[0].key
        assert key.org_id == ORG_HK
        assert plan.org_id == ORG_HK
        assert not hasattr(key, "location_code")
        assert not hasattr(key, "price_date")

    def test_missing_or_invalid_org_blocks_planning(self, tmp_path):
        job_id = approved_job(tmp_path)
        plan = pl.build_plan(job_id, org_id=None)
        assert plan.planning_problems and not plan.lookups
        assert "organization" in plan.planning_problems[0].lower()
        plan = pl.build_plan(job_id, org_id="999999")   # not in catalog
        assert plan.planning_problems and not plan.lookups
        with pytest.raises(JobError, match="organization"):
            pl.run_product_lookup(job_id, auth=FakeAuth(),
                                  transport=FakeTransport([]))


# --- batching ---------------------------------------------------------------------

class TestBatching:
    def test_exact_split_and_final_partial_batch(self):
        keys = [pl.ProductLookupKey(ORG_HK, f"E{i}", "EAN")
                for i in range(7)]
        batches = pl.make_batches(keys, 3)
        assert [len(b) for b in batches] == [3, 3, 1]
        assert batches[0][0].plu == "E0"                # order kept

    def test_positive_batch_size_enforced(self):
        with pytest.raises(pl.ProductError):
            pl.make_batches([], 0)

    def test_batches_split_in_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRODUCT_LOOKUP_BATCH_SIZE", "1")
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(
            job_id,
            [(200, envelope([wire_record(EAN_A)])),
             (200, envelope([wire_record(
                 EAN_B, item="ZETF381237E085", color="E085", size="XS",
                 desc="SRT - JAZZ SHORTS", price=1900.00)]))],
            config=pl.load_product_config())
        assert len(transport.calls) == 2
        assert [b["request_count"]
                for b in enrichment["batches"]] == [1, 1]
        assert enrichment["summary"]["matched_lines"] == 2


# --- authentication integration ---------------------------------------------------

class TestAuthIntegration:
    def test_token_obtained_before_call_and_reused(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setenv("PRODUCT_LOOKUP_BATCH_SIZE", "1")
        job_id = approved_job(tmp_path)
        enrichment, transport, auth = run_lookup(
            job_id,
            [(200, envelope([wire_record(EAN_A)])),
             (200, envelope([wire_record(
                 EAN_B, item="ZETF381237E085", color="E085", size="XS",
                 desc="SRT - JAZZ SHORTS", price=1900.00)]))],
            config=pl.load_product_config())
        assert auth.ensure_calls == 2                   # per batch, cached
        assert all(c["headers"]["Authorization"] == "Bearer tok-A"
                   for c in transport.calls)

    def test_401_recovery_retries_batch_once(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, auth = run_lookup(
            job_id,
            [(401, {"code": None}),
             (200, envelope([wire_record(EAN_A), wire_record(
                 EAN_B, item="ZETF381237E085", color="E085", size="XS",
                 desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        assert auth.unauthorized_calls == 1
        assert len(transport.calls) == 2
        assert transport.calls[1]["headers"]["Authorization"] == "Bearer tok-B"
        assert enrichment["batches"][0]["auth_recovered"] is True
        assert enrichment["status"] == "complete"

    def test_second_401_is_access_denied(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, auth = run_lookup(
            job_id, [(401, None), (401, None)])
        assert enrichment["status"] == "failed"
        assert any(i["code"] == pl.PRODUCT_LOOKUP_ACCESS_DENIED
                   for i in enrichment["issues"])
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PRODUCT_LOOKUP_FAILED)

    def test_auth_failure_creates_safe_typed_issue(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(
            job_id, [], auth=FakeAuth(fail=True))
        assert transport.calls == []                    # API never reached
        assert any(i["code"] == pl.PRODUCT_LOOKUP_AUTH_ERROR
                   for i in enrichment["issues"])

    def test_no_tokens_in_artifact(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        blob = pl.result_path(job_id).read_text()
        for forbidden in ("tok-A", "tok-B", "Bearer", "Authorization",
                          "accessToken", "refreshToken"):
            assert forbidden not in blob, forbidden


# --- request construction ---------------------------------------------------------

class TestRequest:
    def test_exact_request_shape(self, tmp_path):
        job_id = approved_job(tmp_path)
        _, transport, _ = run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        body = transport.calls[0]["body"]
        # spec-exact itemMaster-get: requestList of orgId + plu ONLY -
        # no locationCode (nullable in schema, deliberately omitted), no
        # PriceDate, no Qty
        assert body == {"requestList": [
            {"orgId": ORG_HK, "plu": EAN_A},
            {"orgId": ORG_HK, "plu": EAN_B},
        ]}
        assert transport.calls[0]["url"] == (
            "https://gw.test/devgapi/corpTool/itemMaster-get")


# --- response handling ------------------------------------------------------------

class TestResponse:
    def test_http_200_with_failure_code_fails(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(
            job_id, [(200, envelope([], code=100001))])
        assert enrichment["status"] == "failed"
        assert any(i["code"] == pl.PRODUCT_LOOKUP_API_ERROR
                   for i in enrichment["issues"])

    def test_malformed_json_fails_safely(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, None)])
        assert enrichment["status"] == "failed"

    def test_not_found_is_omission(self, tmp_path):
        # gateway skips unknown combinations: only EAN_A returned; the
        # missing EAN_B then attempts its constructed fallback (also empty)
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(
            job_id, [(200, envelope([wire_record(EAN_A)])),
                     (200, envelope([]))])
        by_line = {l["line_id"]: l
                   for l in enrichment["line_enrichments"]}
        assert by_line["D001-C001-L001"]["status"] == "matched"
        assert by_line["D001-C001-L002"]["status"] == "unmatched"
        assert any(i["code"] == pl.PRODUCT_NOT_FOUND
                   and i["line_id"] == "D001-C001-L002"
                   for i in enrichment["issues"])

    def test_response_order_does_not_matter(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS", price=1900.00),
            wire_record(EAN_A),
        ]))])
        assert enrichment["summary"]["matched_lines"] == 2
        by_line = {l["line_id"]: l for l in enrichment["line_enrichments"]}
        product_a = enrichment["products"][
            by_line["D001-C001-L001"]["product_ref"]]
        assert product_a["ean"] == EAN_A                # correlated by id

    def test_duplicate_matches_flagged(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A), wire_record(EAN_A),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS",
                        price=1900.00)]))])
        assert any(i["code"] == pl.PRODUCT_MULTIPLE_MATCHES
                   for i in enrichment["issues"])
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PRODUCT_LOOKUP_WITH_ISSUES)

    def test_unmatched_record_is_ambiguous(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS", price=1900.00),
            wire_record("9999999999999")]))])
        assert any(i["code"] == pl.PRODUCT_LOOKUP_RESPONSE_AMBIGUOUS
                   for i in enrichment["issues"])

    def test_analysis_codes_and_compositions_parsed(self, tmp_path):
        job_id = approved_job(tmp_path)
        extra = {f"analysisCode{i:02d}": f"A{i}" for i in range(1, 16)}
        extra.update({f"composition{i:02d}": f"C{i}" for i in range(1, 5)})
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A, **extra),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS",
                        price=1900.00)]))])
        product = enrichment["products"][0]
        assert product["analysis_code_01"] == "A1"
        assert product["analysis_code_15"] == "A15"
        assert product["composition_01"] == "C1"
        assert product["composition_04"] == "C4"
        assert product["xf_groups"] == {}       # not in itemMaster-get
        assert product["original_retail_price"] == "1400.0"
        assert product["discount_price"] == "399.0"     # currentPrice
        assert product["ean"] == EAN_A                  # leading zero kept

    def test_live_confirmed_wire_schema_normalizes(self):
        """Build 9 live-confirmed schema, replayed with SYNTHETIC values:
        flat camelCase record; analysisCode01..15 always present with
        blank-as-empty-string; compositions arrive MISSPELLED and unpadded
        as compositon1..compositon4; locationCode/plu echo the request;
        ean keeps leading zeros; qty echoes as an int; prices are JSON
        numbers; unknown fields are preserved in the raw copy."""
        raw = wire_record("FAKE0010001111A1BXYZM", ean="0999990000012345",
                          item="FAKE0010001111A1B", color="A1BXY", size="M")
        raw.update({f"analysisCode{i:02d}": "" for i in range(1, 16)})
        raw.update({"analysisCode01": "FAKE SEASON",
                    "analysisCode02": "FAKE-GROUP",
                    "analysisCode06": "12.5"})
        raw.update({f"compositon{i}": "" for i in range(1, 5)})
        raw.update({"compositon1": "FAKE 90% WOOL",
                    "compositon2": "FAKE 10% SILK"})
        raw.update({"qty": 1, "someFutureField": "kept-safely"})
        product = pl.normalize_record(raw)
        assert product["analysis_code_01"] == "FAKE SEASON"
        assert product["analysis_code_02"] == "FAKE-GROUP"
        assert product["analysis_code_06"] == "12.5"
        assert product["analysis_code_03"] == ""        # blank, not None
        assert product["analysis_code_15"] == ""
        # the misspelled real wire names are captured
        assert product["composition_01"] == "FAKE 90% WOOL"
        assert product["composition_02"] == "FAKE 10% SILK"
        assert product["composition_03"] == ""
        assert product["composition_04"] == ""
        assert product["ean"] == "0999990000012345"     # leading zero kept
        assert product["plu"] == "FAKE0010001111A1BXYZM"
        assert product["org_id"] == ORG_HK              # orgId echo
        assert product["location_code"] is None         # not in itemMaster
        assert product["qty_echo"] == "1"               # tolerated if sent
        assert product["raw"]["someFutureField"] == "kept-safely"

    def test_wrong_org_record_never_matched(self, tmp_path):
        """A response record whose orgId echo differs from the requested
        Organization ID is NEVER matched to a line - it raises a blocking
        PRODUCT_ORG_MISMATCH issue instead."""
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [
            (200, envelope([
                wire_record(EAN_A, org=ORG_TW),          # wrong org
                wire_record(EAN_B, org=ORG_HK, item="ZETF381237E085",
                            color="E085", size="XS",
                            desc="SRT - JAZZ SHORTS", price=1900.00)])),
            (200, envelope([]))])                        # empty fallback
        assert any(i["code"] == pl.PRODUCT_ORG_MISMATCH
                   for i in enrichment["issues"])
        by_line = {l["source"]["ean"]: l
                   for l in enrichment["line_enrichments"]}
        assert by_line[EAN_A]["status"] == "unmatched"   # never mismatched
        assert by_line[EAN_B]["status"] == "matched"

    def test_composition_spelling_variants_all_accepted(self):
        for key, expected_field in (("compositon1", "composition_01"),
                                    ("composition1", "composition_01"),
                                    ("composition01", "composition_01"),
                                    ("Compositon4", "composition_04"),
                                    ("composition #4", "composition_04")):
            product = pl.normalize_record(wire_record("X", **{key: "VAL"}))
            assert product[expected_field] == "VAL", key
        # out-of-range numbers are not analysis/composition fields
        product = pl.normalize_record(wire_record("X", compositon5="V"))
        assert "composition_05" not in product


# --- fallback ---------------------------------------------------------------------

def _fallback_job(tmp_path):
    """Two lines sharing EAN_A (dedup) so fallback is exercised once."""
    dup_rows = (ROW_A, ("2",) + ROW_A[1:])
    return approved_job(tmp_path, [{"rows": dup_rows, "carton_total": None}])


class TestRetryResumeAndRateLimit:
    """Pilot-proven defect (job with 481 lookups): a retry re-ran ALL
    batches from scratch, resending hundreds of successful requests and
    triggering an HTTP 429. Retries must resume: completed logical
    batches are never resent; only failed ones run again with the same
    logical_batch_number and attempt_number + 1; a run lock makes one
    click at most one outbound run; 429 maps to a non-retrying
    rate-limited state."""

    RECORD_A = wire_record(EAN_A)
    RECORD_B = wire_record(EAN_B, item="ZETF381237E085", color="E085",
                           size="XS", desc="SRT - JAZZ SHORTS",
                           price=1900.00)
    ONE_PER_BATCH = pl.ProductLookupConfig(batch_size=1)

    def test_two_identifiers_produce_one_logical_batch(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        assert len(transport.calls) == 1
        assert enrichment["summary"]["batches"] == 1
        batch = enrichment["batches"][0]
        assert batch["logical_batch_number"] == 1
        assert batch["batch_number"] == 1            # legacy alias kept
        assert batch["attempt_number"] == 1
        assert batch["status"] == "success"
        assert batch["started_at"] and batch["completed_at"]

    def test_retry_resends_only_the_failed_batch(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A])), (500, None)],
            config=self.ONE_PER_BATCH)
        assert enrichment["status"] == "failed"
        assert len(transport.calls) == 2
        by_number = {b["logical_batch_number"]: b
                     for b in enrichment["batches"]}
        assert by_number[1]["status"] == "success"
        assert by_number[2]["status"] == "failed"
        assert by_number[2]["error_code"] == pl.PRODUCT_HTTP_ERROR

        # retry: ONLY logical batch 2 goes out; batch 1 is not resent
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_B]))],
            config=self.ONE_PER_BATCH)
        assert len(transport.calls) == 1
        sent = transport.calls[0]["body"]["requestList"]
        assert [item["plu"] for item in sent] == [EAN_B]
        assert enrichment["status"] == "complete"
        assert enrichment["summary"]["matched_lines"] == 2
        by_number = {b["logical_batch_number"]: b
                     for b in enrichment["batches"]}
        assert len(by_number) == 2                    # no duplicates
        assert by_number[1]["attempt_number"] == 1    # preserved
        assert by_number[2]["attempt_number"] == 2    # replaced in place
        assert by_number[2]["status"] == "success"

    def test_ten_retries_increment_attempts_not_batches(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope([self.RECORD_A])), (500, None)],
                   config=self.ONE_PER_BATCH)
        for _ in range(9):
            enrichment, transport, _ = run_lookup(
                job_id, [(500, None)], config=self.ONE_PER_BATCH)
            assert len(transport.calls) == 1          # only the failed one
        assert len(enrichment["batches"]) == 2
        assert sorted(b["logical_batch_number"]
                      for b in enrichment["batches"]) == [1, 2]
        by_number = {b["logical_batch_number"]: b
                     for b in enrichment["batches"]}
        assert by_number[2]["attempt_number"] == 10
        assert enrichment["summary"]["batch_attempts"] == 11

    def test_completed_run_retry_sends_nothing(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id,
                   [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        enrichment, transport, _ = run_lookup(job_id, [])
        assert transport.calls == []                  # zero outbound
        assert enrichment["status"] == "complete"
        assert enrichment["summary"]["matched_lines"] == 2

    def test_rate_limited_maps_and_never_auto_retries(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(
            job_id, [(429, None, {"Retry-After": "120"})])
        assert len(transport.calls) == 1              # no auto retry at all
        assert enrichment["status"] == "failed"
        issue = next(i for i in enrichment["issues"]
                     if i["code"] == pl.PRODUCT_LOOKUP_RATE_LIMITED)
        assert issue["retry_after_seconds"] == 120
        assert "120" in issue["message"]
        batch = enrichment["batches"][0]
        assert batch["status"] == "failed"
        assert batch["http_status"] == 429
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PRODUCT_LOOKUP_FAILED)      # stays retryable

    def test_rate_limit_without_header_still_safe(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, transport, _ = run_lookup(job_id, [(429, None)])
        assert len(transport.calls) == 1              # 429 blocks fallback
        issue = next(i for i in enrichment["issues"]
                     if i["code"] == pl.PRODUCT_LOOKUP_RATE_LIMITED)
        assert issue["retry_after_seconds"] is None

    def test_run_lock_one_click_one_run(self, tmp_path):
        job_id = approved_job(tmp_path)
        lock = pl._run_lock_path(job_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("{}")
        assert pl.lookup_running(job_id) is True
        with pytest.raises(JobError, match="already running"):
            run_lookup(job_id, [(200, envelope([self.RECORD_A,
                                                self.RECORD_B]))])
        # a stale lock from a crashed run is reclaimed
        import os as _os
        old = 4000.0
        _os.utime(lock, (old, old))
        assert pl.lookup_running(job_id) is False
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        assert enrichment["status"] == "complete"
        assert not lock.exists()                      # released after run

    def test_legacy_artifact_without_checkpoints_resets_cleanly(
            self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope([self.RECORD_A])), (500, None)],
                   config=self.ONE_PER_BATCH)
        # simulate a pre-fix artifact: no per-key checkpoints persisted
        path = pl.result_path(job_id)
        legacy = json.loads(path.read_text())
        legacy.pop("key_results", None)
        path.write_text(json.dumps(legacy))
        # a plain retry REFUSES the silent full resend
        with pytest.raises(JobError, match="Restart Product Lookup"):
            run_lookup(job_id, [], config=self.ONE_PER_BATCH)
        # the explicitly confirmed restart performs one clean full run:
        # numbering restarts, no batch-11-style continuation appears
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A])),
                     (200, envelope([self.RECORD_B]))],
            config=self.ONE_PER_BATCH, allow_full_rerun=True)
        assert len(transport.calls) == 2
        assert sorted(b["logical_batch_number"]
                      for b in enrichment["batches"]) == [1, 2]
        assert all(b["attempt_number"] == 1 for b in enrichment["batches"])
        assert enrichment["status"] == "complete"
        # extraction and approved review are untouched by the reset
        review = rv.load_review(job_id)
        assert review is not None

    def test_different_org_never_resumes_checkpoints(self, tmp_path):
        """Organization identity is part of the execution identity:
        checkpoints from 100009 can never be reused for 100012; the same
        org + same input resumes normally; switching org routes through
        the explicit restart confirmation and re-sends everything with
        the new orgId."""
        job_id = approved_job(tmp_path)
        run_lookup(job_id,
                   [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        # same org, same input: full resume - zero outbound requests
        _, transport, _ = run_lookup(job_id, [])
        assert transport.calls == []
        # different org: incompatible -> plain retry refused
        with pytest.raises(JobError, match="Restart Product Lookup"):
            run_lookup(job_id, [], org_id=ORG_TW,
                       org_name="IMAGINEX Taiwan")
        # confirmed restart re-sends ALL lookups for the NEW org
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([
                wire_record(EAN_A, org=ORG_TW),
                wire_record(EAN_B, org=ORG_TW, item="ZETF381237E085",
                            color="E085", size="XS",
                            desc="SRT - JAZZ SHORTS", price=1900.00)]))],
            org_id=ORG_TW, org_name="IMAGINEX Taiwan",
            allow_full_rerun=True)
        assert len(transport.calls) == 1
        sent = transport.calls[0]["body"]["requestList"]
        assert all(item["orgId"] == ORG_TW for item in sent)
        assert enrichment["organization"] == {"id": ORG_TW,
                                              "name": "IMAGINEX Taiwan"}
        assert enrichment["status"] == "complete"
        # and the new-org artifact resumes for the new org
        _, transport, _ = run_lookup(job_id, [], org_id=ORG_TW,
                                     org_name="IMAGINEX Taiwan")
        assert transport.calls == []

    def test_checkpoint_metadata_records_organization(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        raw = json.loads(pl.result_path(job_id).read_text())
        assert raw["organization"] == {"id": ORG_HK,
                                       "name": "IMAGINEX Hong Kong"}
        assert raw["plan"]["organization_id"] == ORG_HK
        assert raw["key_results"]
        assert all(entry["key"]["org_id"] == ORG_HK
                   for entry in raw["key_results"])

    def test_legacy_artifact_without_org_identity_not_resumed(
            self, tmp_path):
        """Pre-Build-11 artifacts (checkpoints but no organization) are
        legacy/incompatible: never silently resumed, only the explicit
        confirmed restart runs - and nothing deletes the legacy file."""
        job_id = approved_job(tmp_path)
        run_lookup(job_id,
                   [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        path = pl.result_path(job_id)
        legacy = json.loads(path.read_text())
        legacy.pop("organization", None)         # simulate pre-Build-11
        path.write_text(json.dumps(legacy))
        with pytest.raises(JobError, match="Restart Product Lookup"):
            run_lookup(job_id, [])
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A, self.RECORD_B]))],
            allow_full_rerun=True)
        assert len(transport.calls) == 1         # clean full rerun
        assert enrichment["status"] == "complete"
        assert enrichment["organization"]["id"] == ORG_HK

    def test_review_change_invalidates_resume(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope([self.RECORD_A])), (500, None)],
                   config=self.ONE_PER_BATCH)
        review = rv.load_review(job_id)
        rv.apply_correction(review, "line", review.lines[0].entity_id,
                            "description", "EDITED AFTER FAILURE")
        rv.save_review(job_id, review)
        rv.approve_review(job_id)
        # the invalidated resume also requires the explicit confirmation
        with pytest.raises(JobError, match="Restart Product Lookup"):
            run_lookup(job_id, [], config=self.ONE_PER_BATCH)
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A])),
                     (200, envelope([self.RECORD_B]))],
            config=self.ONE_PER_BATCH, allow_full_rerun=True)
        assert len(transport.calls) == 2              # full clean rerun
        assert enrichment["status"] == "complete"

    def test_failed_first_run_needs_no_confirmation(self, tmp_path):
        """A prior artifact with NO successful batches (or none at all)
        retries plainly - the confirmation gate is only for reruns that
        would resend completed work."""
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(429, None)])             # nothing succeeded
        enrichment, transport, _ = run_lookup(
            job_id, [(200, envelope([self.RECORD_A, self.RECORD_B]))])
        assert enrichment["status"] == "complete"
        assert len(transport.calls) == 1

    def test_resumable_artifact_never_asks_for_confirmation(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope([self.RECORD_A])), (500, None)],
                   config=self.ONE_PER_BATCH)
        plan = pl.build_plan(job_id, self.ONE_PER_BATCH, org_id=ORG_HK)
        assert pl.requires_full_rerun_confirmation(job_id, plan) is False


class TestFallback:
    def test_ean_not_found_then_constructed_succeeds(self, tmp_path):
        job_id = _fallback_job(tmp_path)
        enrichment, transport, _ = run_lookup(job_id, [
            (200, envelope([])),                        # EAN stage: nothing
            (200, envelope([wire_record(CONSTRUCTED_A, ean=EAN_A)])),
        ])
        assert len(transport.calls) == 2
        assert transport.calls[1]["body"]["requestList"][0]["plu"] == \
            CONSTRUCTED_A
        lines = enrichment["line_enrichments"]
        assert all(l["status"] == "matched" for l in lines)
        assert all(l["matched_via"] == "CONSTRUCTED" for l in lines)
        assert all(len(l["attempts"]) == 2 for l in lines)
        assert enrichment["summary"]["matched_via_fallback"] == 2

    def test_both_attempts_not_found(self, tmp_path):
        job_id = _fallback_job(tmp_path)
        enrichment, transport, _ = run_lookup(job_id, [
            (200, envelope([])), (200, envelope([]))])
        assert len(transport.calls) == 2                # exactly two stages
        assert all(l["status"] == "unmatched"
                   for l in enrichment["line_enrichments"])
        assert any(i["code"] == pl.PRODUCT_NOT_FOUND
                   for i in enrichment["issues"])

    def test_no_fallback_after_auth_or_invalid_response(self, tmp_path):
        job_id = _fallback_job(tmp_path)
        enrichment, transport, _ = run_lookup(job_id, [(200, None)])
        assert len(transport.calls) == 1                # no fallback stage
        assert enrichment["status"] == "failed"

    def test_fallback_deduplicated(self, tmp_path):
        job_id = _fallback_job(tmp_path)
        _, transport, _ = run_lookup(job_id, [
            (200, envelope([])), (200, envelope([]))])
        assert len(transport.calls[1]["body"]["requestList"]) == 1


# --- comparison -------------------------------------------------------------------

class TestComparison:
    def test_exact_match_no_issues(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        assert enrichment["summary"]["blocking_issues"] == 0
        assert enrichment["summary"]["warning_issues"] == 0
        assert enrichment["status"] == "complete"

    def test_identity_mismatches_blocking(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A, item="OTHERITEM99", color="E001", size="L"),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS",
                        price=1900.00)]))])
        codes = {i["code"] for i in enrichment["issues"]}
        assert {pl.PRODUCT_ITEM_MISMATCH, pl.PRODUCT_COLOR_MISMATCH,
                pl.PRODUCT_SIZE_MISMATCH} <= codes
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PRODUCT_LOOKUP_WITH_ISSUES)

    def test_description_and_price_warnings(self, tmp_path):
        job_id = approved_job(tmp_path)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A, desc="DIFFERENT WORDING", price=999.00),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS",
                        price=1900.00)]))])
        warn = [i for i in enrichment["issues"]
                if i["severity"] == pl.SEV_WARNING]
        codes = {i["code"] for i in warn}
        assert pl.PRODUCT_DESCRIPTION_MISMATCH in codes
        assert pl.PRODUCT_RETAIL_PRICE_MISMATCH in codes
        assert enrichment["status"] == "complete"       # warnings only

    def test_api_values_never_overwrite_review(self, tmp_path):
        job_id = approved_job(tmp_path)
        before = rv.review_path(job_id).read_bytes()
        run_lookup(job_id, [(200, envelope([
            wire_record(EAN_A, item="OTHERITEM99"),
            wire_record(EAN_B, item="ZETF381237E085", color="E085",
                        size="XS", desc="SRT - JAZZ SHORTS",
                        price=1900.00)]))])
        assert rv.review_path(job_id).read_bytes() == before
        enrichment = pl.load_enrichment(job_id)
        line = enrichment["line_enrichments"][0]
        assert line["source"]["item_code"] == "ZEHE380331E997"  # both stored
        product = enrichment["products"][line["product_ref"]]
        assert product["item_code"] == "OTHERITEM99"

    def test_source_missing_field_api_provides_no_mismatch(self, tmp_path):
        job_id = approved_job(tmp_path)
        review = rv.load_review(job_id)
        from apps.web.transfer.review_models import CLEAR
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            CLEAR)
        rv.save_review(job_id, review)
        rv.approve_review(job_id)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        assert not any(i["code"] == pl.PRODUCT_SIZE_MISMATCH
                       for i in enrichment["issues"])
        line = enrichment["line_enrichments"][0]
        product = enrichment["products"][line["product_ref"]]
        assert product["size_code"] == "S"              # API resolves it


# --- persistence + states ---------------------------------------------------------

class TestPersistence:
    def _ok(self, tmp_path):
        job_id = approved_job(tmp_path)
        run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        return job_id

    def test_schema_atomic_reload(self, tmp_path):
        job_id = self._ok(tmp_path)
        raw = json.loads(pl.result_path(job_id).read_text())
        assert raw["schema_version"] == 1
        assert raw["review_checksum"]
        assert not list(pl.result_path(job_id).parent.glob("*.tmp-*"))
        reloaded = pl.load_enrichment(job_id)
        assert reloaded["stale"] is False
        assert reloaded["summary"]["matched_lines"] == 2
        assert (tjobs.load_transfer_job(job_id).status
                == tm.JOB_PRODUCT_LOOKUP_COMPLETE)

    def test_retry_replaces_without_duplication(self, tmp_path):
        job_id = self._ok(tmp_path)
        run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        reloaded = pl.load_enrichment(job_id)
        assert reloaded["summary"]["lines"] == 2        # not doubled
        assert len(list(pl.result_path(job_id).parent.glob("*.json"))) == 1

    def test_review_change_marks_stale_and_archives_on_retry(self, tmp_path):
        job_id = self._ok(tmp_path)
        review = rv.load_review(job_id)
        rv.apply_correction(review, "line", "D001-C001-L001", "size_code",
                            "M")
        rv.save_review(job_id, review)
        assert pl.load_enrichment(job_id)["stale"] is True
        rv.approve_review(job_id)
        # a stale prior with completed batches requires the explicit
        # confirmed restart (retry-storm protection)
        run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A, size="M"), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))],
            allow_full_rerun=True)
        archived = list(pl.result_path(job_id).parent.glob(
            "result-stale-*.json"))
        assert len(archived) == 1                       # audit copy kept
        assert pl.load_enrichment(job_id)["stale"] is False

    def test_malformed_artifact_fails_safely(self, tmp_path):
        job_id = self._ok(tmp_path)
        pl.result_path(job_id).write_text("{ nope")
        assert pl.load_enrichment(job_id) is None
        assert extraction.load_result(job_id) is not None

    def test_stale_in_progress_can_retry(self, tmp_path):
        job_id = approved_job(tmp_path)
        tjobs.update_job_status(job_id, tm.JOB_PRODUCT_LOOKUP_IN_PROGRESS)
        enrichment, _, _ = run_lookup(job_id, [(200, envelope(
            [wire_record(EAN_A), wire_record(
                EAN_B, item="ZETF381237E085", color="E085", size="XS",
                desc="SRT - JAZZ SHORTS", price=1900.00)]))])
        assert enrichment["status"] == "complete"

    def test_invalid_transition_rejected(self, tmp_path):
        job_id = approved_job(tmp_path)
        with pytest.raises(JobError):
            tjobs.update_job_status(job_id, tm.JOB_PRODUCT_LOOKUP_COMPLETE)


# --- UI + boundaries (static) -----------------------------------------------------

class TestUiAndBoundaries:
    RPAGE = (ROOT / "apps" / "web" / "transfer" / "review_page.py").read_text(
        encoding="utf-8")
    PL = (ROOT / "apps" / "web" / "transfer" / "product_lookup.py").read_text(
        encoding="utf-8")

    def test_run_button_gated(self):
        assert "Run Product Lookup" in self.RPAGE
        assert "disabled=run_disabled" in self.RPAGE
        assert "_PRODUCT_STATES" in self.RPAGE

    def test_summary_and_attribute_columns_present(self):
        assert "Lookup summary" in self.RPAGE
        # Build 10: Analysis/Composition columns surface through the shared
        # enriched-export schema in the final enriched lines table
        assert "Final enriched product lines" in self.RPAGE
        from apps.web.transfer import enriched_export as ex
        headers = [h for h, _ in ex.EXPORT_COLUMNS]
        assert "AC01" in headers and "AC15" in headers
        assert "Composition 1" in headers and "Composition 4" in headers

    def test_no_excel_or_resequencing_controls(self):
        # scope to the unsanctioned sections: the Build 7 workbook section
        # and the Build 10 enriched-lines export renderer hold the
        # sanctioned Excel downloads
        before, rest = self.RPAGE.split("def _render_enriched_lines_table",
                                        1)
        after_enriched = "def " + rest.split("\ndef ", 1)[1]
        page_part = before + after_enriched.split(
            "def _render_workbook_section")[0]
        low = (page_part + self.PL).lower()
        for forbidden in ("openpyxl", "xlsx", "resequenc", "zipfile",
                          "download_button"):
            assert forbidden not in low, forbidden

    def test_no_consolidation_in_build5(self):
        assert "consolidate" not in self.PL.lower()
        # itemMaster-get requests carry ONLY orgId + plu: quantities are
        # never sent, never summed, and rows are never merged
        assert '"orgId": self.org_id, "plu": self.plu' in self.PL
        request_item = self.PL.split("def request_item", 1)[1]
        request_item = request_item.split("def ", 1)[0]
        for absent in ('"Qty"', '"PriceDate"', '"locationCode"',
                       '"LocationCode"'):
            assert absent not in request_item, absent

    def test_tokens_never_rendered(self):
        for forbidden in ("access_token", "get_authorization_header",
                          "ensure_access_token"):
            assert forbidden not in self.RPAGE, forbidden

    def test_invoice_workflow_untouched(self):
        for name in ("job_manager.py", "worker.py", "app.py"):
            src = (ROOT / "apps" / "web" / name).read_text(encoding="utf-8")
            assert "product_lookup" not in src, name
