"""End-to-end pipeline tests with mocked provider seams (no network, no keys).

The seams are gemini_client._generate(cfg, model, contents) and
claude_client._request(cfg, model, content): everything above them - prompt
construction, JSON parsing, schema validation, fallback policy, aggregation,
provenance - runs for real.
"""

import json
from pathlib import Path

import pytest
from google.genai import errors as genai_errors

from invoice_extractor import claude_client, gemini_client
from invoice_extractor.pipeline import process_directory, process_file

from .conftest import TEXT_BODY, build_pdf, invoice_json, make_config


def server_error():
    return genai_errors.APIError(503, {"error": {"message": "synthetic overload"}})


class Recorder:
    """Callable seam replacement recording calls; returns queued responses."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, cfg, model, contents):
        self.calls.append(model)
        if not self.responses:
            raise AssertionError("provider called more times than expected")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def text_pdf(pdf_factory):
    return Path(pdf_factory([("text", TEXT_BODY)], name="text.pdf"))


@pytest.fixture
def scan_pdf(pdf_factory):
    return Path(pdf_factory([("image",)], name="scan.pdf"))


@pytest.fixture
def mixed_pdf(pdf_factory):
    return Path(pdf_factory([("text", TEXT_BODY), ("image",), ("blank",)], name="mixed.pdf"))


class TestTextRoute:
    def test_gemini_text_success(self, cfg, logger, text_pdf, monkeypatch):
        gem = Recorder([invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        result = process_file(text_pdf, cfg, logger)
        assert result.document_classification == "text-native"
        assert result.extraction_method == "text"
        assert result.provider == "gemini"
        assert result.model == cfg.gemini_text_model
        assert gem.calls == [cfg.gemini_text_model]
        assert result.needs_review is False
        assert result.invoice.invoice_number == "INV-1001"

    def test_gemini_failure_fallback_disabled_never_calls_claude(
        self, logger, text_pdf, monkeypatch
    ):
        cfg = make_config(enable_claude_text_fallback=False, max_retries=2)
        monkeypatch.setattr(gemini_client, "_generate",
                            Recorder([server_error(), server_error()]))
        claude_calls = []
        monkeypatch.setattr(claude_client, "_request",
                            lambda *a, **k: claude_calls.append(1))
        result = process_file(text_pdf, cfg, logger)
        assert claude_calls == []  # Claude must not be touched
        assert result.extraction_method == "failed"
        assert result.provider == "none"
        assert result.needs_review is True and result.error is True
        assert "text route" in result.review_reason
        assert "failed on all providers" in result.review_reason
        assert result.failed_pages == [1]

    def test_gemini_failure_fallback_enabled_uses_claude(
        self, logger, text_pdf, monkeypatch
    ):
        cfg = make_config(enable_claude_text_fallback=True, max_retries=1)
        monkeypatch.setattr(gemini_client, "_generate", Recorder([server_error()]))
        claude = Recorder([invoice_json()])
        monkeypatch.setattr(claude_client, "_request", claude)
        result = process_file(text_pdf, cfg, logger)
        assert claude.calls == [cfg.claude_text_model]
        assert result.extraction_method == "text"
        assert result.provider == "claude"  # provenance distinguishes the fallback
        assert result.model == cfg.claude_text_model
        assert result.needs_review is False


class TestVisionRoute:
    def test_gemini_vision_success(self, cfg, logger, scan_pdf, monkeypatch):
        gem = Recorder([invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        result = process_file(scan_pdf, cfg, logger)
        assert result.document_classification == "image-only"
        assert result.extraction_method == "vision"
        assert result.provider == "gemini"
        assert result.model == cfg.gemini_vision_model
        assert gem.calls == [cfg.gemini_vision_model]

    def test_gemini_malformed_json_falls_back_to_claude(
        self, cfg, logger, scan_pdf, monkeypatch
    ):
        # Second queued response is the one JSON-repair retry gemini_client
        # now attempts before giving up - also malformed, so Claude still
        # ends up as the fallback exactly as before this milestone.
        gem = Recorder(['{"invoice_number": broken json', 'still not json {'])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        claude = Recorder([invoice_json()])
        monkeypatch.setattr(claude_client, "_request", claude)
        result = process_file(scan_pdf, cfg, logger)
        assert gem.calls == [cfg.gemini_vision_model, cfg.gemini_vision_model]
        assert claude.calls == [cfg.claude_vision_model]
        assert result.provider == "claude"
        assert result.extraction_method == "vision"
        assert result.needs_review is False

    def test_gemini_schema_invalid_falls_back_to_claude(
        self, cfg, logger, scan_pdf, monkeypatch
    ):
        # Valid JSON but required fields null -> schema failure -> Claude
        invalid = json.dumps({"invoice_number": None, "total_amount": None})
        monkeypatch.setattr(gemini_client, "_generate", Recorder([invalid]))
        claude = Recorder([invoice_json()])
        monkeypatch.setattr(claude_client, "_request", claude)
        result = process_file(scan_pdf, cfg, logger)
        assert result.provider == "claude"
        assert result.invoice.total_amount is not None

    def test_both_vision_providers_fail_emits_review_row(
        self, logger, scan_pdf, monkeypatch
    ):
        cfg = make_config(max_retries=1)
        monkeypatch.setattr(gemini_client, "_generate", Recorder([server_error()]))
        monkeypatch.setattr(
            claude_client, "_request",
            Recorder(['{"still": "missing required fields"}']),
        )
        result = process_file(scan_pdf, cfg, logger)
        assert result.extraction_method == "failed"
        assert result.provider == "none"
        assert result.needs_review is True and result.error is True
        assert "vision route" in result.review_reason
        assert "failed on all providers" in result.review_reason
        assert result.failed_pages == [1]
        assert result.vision_chunk_count == 1


class TestGeminiJsonRepair:
    """gemini_client's one-shot JSON-repair retry, sitting between local
    cleanup (parse_json_response's own fence-stripping/outer-brace
    extraction - already covered elsewhere) and the existing Claude-fallback
    /needs_review behavior, which this must never change the OUTCOME of."""

    def test_fenced_response_recovered_locally_no_repair_call_needed(
        self, cfg, logger, text_pdf, monkeypatch
    ):
        # Local cleanup (not the new repair retry) handles this - exactly
        # one Gemini call, no fallback.
        gem = Recorder([f"```json\n{invoice_json()}\n```"])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        claude_calls = []
        monkeypatch.setattr(claude_client, "_request",
                            lambda *a, **k: claude_calls.append(1))
        result = process_file(text_pdf, cfg, logger)
        assert gem.calls == [cfg.gemini_text_model]  # exactly one call
        assert claude_calls == []
        assert result.provider == "gemini"
        assert result.needs_review is False

    def test_malformed_response_repaired_via_retry_succeeds_without_claude(
        self, cfg, logger, text_pdf, monkeypatch
    ):
        gem = Recorder(["not valid json at all", invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        claude_calls = []
        monkeypatch.setattr(claude_client, "_request",
                            lambda *a, **k: claude_calls.append(1))
        result = process_file(text_pdf, cfg, logger)
        assert gem.calls == [cfg.gemini_text_model, cfg.gemini_text_model]
        assert claude_calls == []  # repair succeeded - Claude never needed
        assert result.provider == "gemini"
        assert result.needs_review is False
        assert result.invoice.invoice_number == "INV-1001"

    def test_repair_fails_then_falls_back_to_claude_text(
        self, logger, text_pdf, monkeypatch
    ):
        cfg = make_config(enable_claude_text_fallback=True)
        gem = Recorder(["not valid json at all", "still not valid json"])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        claude = Recorder([invoice_json()])
        monkeypatch.setattr(claude_client, "_request", claude)
        result = process_file(text_pdf, cfg, logger)
        assert gem.calls == [cfg.gemini_text_model, cfg.gemini_text_model]
        assert claude.calls == [cfg.claude_text_model]
        assert result.provider == "claude"
        assert result.needs_review is False

    def test_repair_fails_without_fallback_produces_clean_failure(
        self, logger, text_pdf, monkeypatch
    ):
        cfg = make_config(enable_claude_text_fallback=False)
        gem = Recorder(["not valid json at all", "still not valid json"])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        claude_calls = []
        monkeypatch.setattr(claude_client, "_request",
                            lambda *a, **k: claude_calls.append(1))
        result = process_file(text_pdf, cfg, logger)
        assert gem.calls == [cfg.gemini_text_model, cfg.gemini_text_model]
        assert claude_calls == []  # fallback disabled - never called
        assert result.extraction_method == "failed"
        assert result.needs_review is True and result.error is True
        assert "text route" in result.review_reason
        assert "failed on all providers" in result.review_reason

    def test_vision_repair_resends_the_same_images(
        self, cfg, logger, scan_pdf, monkeypatch
    ):
        # The repair call must re-include the original image(s), not just
        # text, both for repair quality and so it's still classified as a
        # vision call by any contents-length-based test seam.
        contents_lengths = []

        def fake(cfg_, model, contents):
            contents_lengths.append(len(contents))
            if len(contents_lengths) == 1:
                return "not valid json at all"
            return invoice_json()

        monkeypatch.setattr(gemini_client, "_generate", fake)
        claude_calls = []
        monkeypatch.setattr(claude_client, "_request",
                            lambda *a, **k: claude_calls.append(1))
        result = process_file(scan_pdf, cfg, logger)
        assert len(contents_lengths) == 2
        assert contents_lengths[0] == contents_lengths[1]  # same image count both times
        assert contents_lengths[0] > 1  # prompt/repair text + at least one image
        assert claude_calls == []
        assert result.provider == "gemini"
        assert result.needs_review is False

    def test_repair_steps_are_logged(self, cfg, logger, text_pdf, monkeypatch, caplog):
        # gemini_client logs to its own module logger ("invoice_extractor"),
        # independent of the "logger" fixture passed into process_file.
        gem = Recorder(["not valid json at all", invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        with caplog.at_level("DEBUG", logger="invoice_extractor"):
            process_file(text_pdf, cfg, logger)
        messages = " ".join(r.message for r in caplog.records)
        assert "not valid JSON" in messages
        assert "repair" in messages.lower()
        assert "recovered valid JSON" in messages

    def test_raw_response_and_secrets_never_appear_in_logs(
        self, logger, text_pdf, monkeypatch, caplog
    ):
        cfg = make_config(gemini_api_key="SECRET-GEM-KEY-SHOULD-NOT-LOG")
        secret_marker = "UNIQUE-RAW-RESPONSE-MARKER-XYZ"
        gem = Recorder([f"not valid json {secret_marker}", "still not valid json either"])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        with caplog.at_level("DEBUG", logger="invoice_extractor"):
            process_file(text_pdf, cfg, logger)
        messages = " ".join(r.message for r in caplog.records)
        assert secret_marker not in messages  # raw response text never logged
        assert "SECRET-GEM-KEY-SHOULD-NOT-LOG" not in messages
        assert "not valid json" not in messages  # the literal response body itself


class TestMixedPdf:
    def test_pages_route_independently_and_merge(self, cfg, logger, mixed_pdf, monkeypatch):
        def fake_generate(cfg_, model, contents):
            if model == cfg.gemini_text_model:
                return invoice_json()  # text route sees page 1
            return invoice_json(line_items=[
                {"description": "Customs handling", "quantity": 2,
                 "unit_price": 9.5, "amount": 19.0},
            ], subtotal=None, tax_amount=0, total_amount=19.0)

        monkeypatch.setattr(gemini_client, "_generate", fake_generate)
        result = process_file(mixed_pdf, cfg, logger)
        assert result.document_classification == "mixed"
        assert result.extraction_method == "mixed"
        assert result.text_pages == [1]
        assert result.image_pages == [2]
        assert result.blank_pages == [3]
        assert result.provider == "gemini"
        assert result.model == f"{cfg.gemini_text_model}+{cfg.gemini_vision_model}"
        # line items from both routes, text route (page 1) first
        descriptions = [it.description for it in result.invoice.line_items]
        assert descriptions == ["Ocean freight", "Customs handling"]
        # conflicting totals (119 vs 19) must be flagged, not silently merged
        assert result.needs_review is True
        assert "conflict in total_amount" in result.review_reason

    def test_conflicting_invoice_numbers_flag_multi_invoice(
        self, cfg, logger, mixed_pdf, monkeypatch
    ):
        def fake_generate(cfg_, model, contents):
            if model == cfg.gemini_text_model:
                return invoice_json(invoice_number="INV-A")
            return invoice_json(invoice_number="INV-B")

        monkeypatch.setattr(gemini_client, "_generate", fake_generate)
        result = process_file(mixed_pdf, cfg, logger)
        assert result.needs_review is True
        assert "multiple invoices" in result.review_reason

    def test_partial_route_failure_keeps_other_route(
        self, logger, mixed_pdf, monkeypatch
    ):
        cfg = make_config(max_retries=1)

        def fake_generate(cfg_, model, contents):
            if model == cfg.gemini_text_model:
                return invoice_json()
            raise server_error()

        monkeypatch.setattr(gemini_client, "_generate", fake_generate)
        monkeypatch.setattr(claude_client, "_request", Recorder([server_error_claude()]))
        result = process_file(mixed_pdf, cfg, logger)
        assert result.extraction_method == "text"  # only surviving route
        assert result.invoice.invoice_number == "INV-1001"
        assert result.needs_review is True
        assert "partial extraction" in result.review_reason


def server_error_claude():
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.InternalServerError(
        "synthetic", response=httpx.Response(529, request=req), body=None
    )


class TestFailureContainment:
    def test_missing_api_keys_produce_controlled_result(self, logger, text_pdf):
        cfg = make_config(gemini_api_key=None, anthropic_api_key=None)
        result = process_file(text_pdf, cfg, logger)
        assert result.extraction_method == "failed"
        assert result.needs_review is True and result.error is True
        assert "GEMINI_API_KEY" in result.review_reason  # name only, no value

    def test_all_blank_document(self, cfg, logger, pdf_factory):
        path = Path(pdf_factory([("blank",)], name="blank.pdf"))
        result = process_file(path, cfg, logger)
        assert result.document_classification == "error"
        assert "no meaningful pages" in result.review_reason

    def test_corrupt_pdf_and_batch_continuation(
        self, cfg, logger, tmp_path, monkeypatch
    ):
        corrupt = tmp_path / "a_corrupt.pdf"
        corrupt.write_bytes(b"garbage bytes, not a pdf")
        build_pdf(tmp_path / "b_good.pdf", [("text", TEXT_BODY)])
        monkeypatch.setattr(gemini_client, "_generate", Recorder([invoice_json()]))

        results = process_directory(tmp_path, cfg, logger)
        assert len(results) == 2
        corrupt_result = next(r for r in results if r.source_file == "a_corrupt.pdf")
        good_result = next(r for r in results if r.source_file == "b_good.pdf")
        assert corrupt_result.error is True
        assert "unreadable PDF" in corrupt_result.review_reason
        assert good_result.error is False
        assert good_result.invoice.invoice_number == "INV-1001"
