"""Deterministic offline full-pipeline tests: FAILURES, FALLBACK, CALL
ACCOUNTING, PRIVACY, AND BATCH CONTINUATION (Milestone 4 Parts E-H).

Same architecture as test_synthetic_pipeline.py: only the provider boundary
(gemini_client._generate / claude_client._request) is mocked via
provider_responses.install_provider_seams; everything else in
invoice_extractor.pipeline runs for real. No network calls, no real retry
sleeps (autouse no_retry_sleep fixture), no .env required.
"""

import anthropic
import httpx
import pytest
from google.genai import errors as genai_errors

from invoice_extractor.logging_setup import new_run_id, setup_logging
from invoice_extractor.pipeline import process_directory, process_file

from .conftest import build_pdf, make_config
from .synthetic_fixtures import ground_truth as gt
from .synthetic_fixtures import provider_responses as pr


# ---------------------------------------------------------------------------
# Synthetic exception constructors (mirrors tests/test_retry.py's pattern -
# these import provider SDK types directly in the TEST FILE, which is fine;
# provider_responses.py itself must not, and does not)
# ---------------------------------------------------------------------------

def gemini_server_error() -> genai_errors.APIError:
    return genai_errors.APIError(503, {"error": {"message": "synthetic overload"}})


def gemini_auth_error() -> genai_errors.APIError:
    return genai_errors.APIError(401, {"error": {"message": "synthetic auth failure"}})


def anthropic_status_error(cls, status: int):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return cls("synthetic", response=resp, body=None)


# ---------------------------------------------------------------------------
# Part E.1-2 - text-route Gemini failure, Claude fallback disabled/enabled
# ---------------------------------------------------------------------------

class TestTextFallbackPolicy:
    def test_gemini_text_failure_fallback_disabled_claude_never_called(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=1, enable_claude_text_fallback=False)
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[gemini_server_error()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1
        assert recorder.claude_text_count == 0  # never called - fallback disabled
        assert result.needs_review is True
        assert result.error is True
        assert result.extraction_method == "failed"
        assert result.provider == "none"

    def test_gemini_text_failure_fallback_enabled_claude_called_provenance_shows_claude(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=1, enable_claude_text_fallback=True)
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_text=[gemini_server_error()],
            claude_text=[pr.invoice_response_json(gt.FIXTURE_01)],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1
        assert recorder.claude_text_count == 1
        assert result.provider == "claude"  # provenance distinguishes the fallback
        assert result.model == cfg.claude_text_model
        assert result.needs_review is False


# ---------------------------------------------------------------------------
# Part E.3-5 - vision-route Gemini failure (3 distinct causes), Claude succeeds
# ---------------------------------------------------------------------------

class TestVisionFallbackCauses:
    """Vision fallback is ALWAYS on (unlike text), regardless of
    enable_claude_text_fallback. Uses fixture 9 at MAX_VISION_PAGES=5 so
    both image pages arrive in a single chunk/call, keeping each test to
    exactly one Gemini vision attempt and one Claude vision attempt."""

    def _run(self, synthetic_fixture_paths, logger, monkeypatch, *gemini_responses):
        cfg = make_config(max_retries=1, max_vision_pages=5)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_vision=list(gemini_responses),
            claude_vision=[pr.fixture_09_chunk_response_json([0, 1, 2], "650.00")],
        )
        return process_file(path, cfg, logger), recorder

    def test_malformed_json_falls_back_to_claude_vision(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        # Second entry is the one JSON-repair retry gemini_client now
        # attempts before giving up - also malformed, so Claude still ends
        # up as the fallback exactly as before.
        result, recorder = self._run(
            synthetic_fixture_paths, logger, monkeypatch,
            pr.malformed_json_text(), pr.malformed_json_text(),
        )
        assert recorder.gemini_vision_count == 2
        assert recorder.claude_vision_count == 1
        assert result.provider == "claude"
        assert result.needs_review is False

    def test_schema_invalid_falls_back_to_claude_vision(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        # Valid JSON, just missing required fields - no JSON-repair retry is
        # attempted at all (repair only fires on a parse failure).
        result, recorder = self._run(
            synthetic_fixture_paths, logger, monkeypatch, pr.missing_required_fields_json(),
        )
        assert recorder.gemini_vision_count == 1
        assert recorder.claude_vision_count == 1
        assert result.provider == "claude"
        assert result.needs_review is False

    def test_timeout_falls_back_to_claude_vision(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        # _generate itself raises - never reaches JSON parsing/repair at all.
        result, recorder = self._run(
            synthetic_fixture_paths, logger, monkeypatch, TimeoutError("synthetic timeout"),
        )
        assert recorder.gemini_vision_count == 1
        assert recorder.claude_vision_count == 1
        assert result.provider == "claude"
        assert result.needs_review is False


# ---------------------------------------------------------------------------
# Part E.6 - both vision providers fail for ONE chunk; later chunks continue
# ---------------------------------------------------------------------------

class TestPartialChunkFailure:
    def test_both_providers_fail_one_chunk_later_chunks_continue(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=1, max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_02_multipage_scanned_exceeds_limit"]
        scenario = gt.FIXTURE_02
        gemini_responses = [
            pr.invoice_response_json_subset(scenario, slice(0, 2)),  # chunk 1 [1,2] OK
            gemini_server_error(),                                   # chunk 2 [3,4] FAILS
            pr.invoice_response_json_subset(scenario, slice(4, 6)),  # chunk 3 [5,6] OK
            pr.invoice_response_json_subset(scenario, slice(6, 7)),  # chunk 4 [7] OK
        ]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_vision=gemini_responses,
            claude_vision=[anthropic_status_error(anthropic.InternalServerError, 529)],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_vision_count == 4  # all 4 chunks attempted
        assert recorder.claude_vision_count == 1  # fallback tried only for the failed chunk
        assert result.vision_chunk_count == 4
        assert result.failed_pages == [3, 4]
        assert result.needs_review is True
        assert result.error is False  # partial result, not a hard failure
        assert "pages 3-4" in result.review_reason
        # partial successful data retained, in page order, failed chunk's
        # items simply absent
        descriptions = [li.description for li in result.invoice.line_items]
        expected = [scenario.expected_line_items[i].description for i in (0, 1, 4, 5, 6)]
        assert descriptions == expected


# ---------------------------------------------------------------------------
# Part E.7 - first file fails, second file still processes
# ---------------------------------------------------------------------------

class TestBatchContinuationAfterFileFailure:
    def test_corrupt_pdf_then_good_pdf_both_get_results(
        self, tmp_path, cfg, logger, monkeypatch
    ):
        corrupt = tmp_path / "a_corrupt.pdf"
        corrupt.write_bytes(b"not a pdf, just garbage bytes")
        good_path = tmp_path / "b_good.pdf"
        from .synthetic_fixtures import scenarios as sc
        sc.build_scenario("fixture_01_multipage_text_native", good_path)

        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(gt.FIXTURE_01)],
        )

        results = process_directory(tmp_path, cfg, logger)

        assert len(results) == 2
        corrupt_result = next(r for r in results if r.source_file == "a_corrupt.pdf")
        good_result = next(r for r in results if r.source_file == "b_good.pdf")
        assert corrupt_result.error is True
        assert "unreadable PDF" in corrupt_result.review_reason
        assert good_result.error is False
        assert good_result.invoice.invoice_number == "INV-3001"
        assert recorder.gemini_text_count == 1  # only the good file reached the provider


# ---------------------------------------------------------------------------
# Part E.8-9 - missing API keys
# ---------------------------------------------------------------------------

class TestMissingApiKeys:
    def test_missing_gemini_key_controlled_result_no_seam_needed(
        self, synthetic_fixture_paths, logger
    ):
        # No install_provider_seams() at all: the REAL (unmocked)
        # gemini_client._get_model() raises RuntimeError before ever
        # reaching _generate, so nothing here needs a provider double.
        cfg = make_config(gemini_api_key=None, anthropic_api_key=None, max_retries=1)
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]

        result = process_file(path, cfg, logger)

        assert result.needs_review is True
        assert result.error is True
        assert "GEMINI_API_KEY" in result.review_reason  # names the var, not any value

    def test_missing_anthropic_key_when_vision_fallback_needed(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=1, max_vision_pages=5, anthropic_api_key=None)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        # No claude_vision responses queued at all - if the real code tried
        # to call the seam despite the missing key, the recorder would
        # raise "unexpected extra call"; instead the REAL (unmocked)
        # claude_client._get_client() raises RuntimeError first, so the
        # seam is never reached and claude_vision_count stays 0.
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_vision=[gemini_server_error()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_vision_count == 1
        assert recorder.claude_vision_count == 0
        assert result.needs_review is True
        assert result.error is True
        assert "ANTHROPIC_API_KEY" in result.review_reason


# ---------------------------------------------------------------------------
# Part E.10 - authentication/invalid-model errors do not retry unnecessarily
# ---------------------------------------------------------------------------

class TestNonTransientErrorsDoNotRetry:
    def test_gemini_auth_error_attempted_exactly_once_despite_generous_retry_budget(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=5, enable_claude_text_fallback=False)
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[gemini_auth_error()],
        )

        result = process_file(path, cfg, logger)

        assert recorder.gemini_text_count == 1  # NOT 5 - auth errors are non-transient
        assert result.needs_review is True

    def test_claude_invalid_model_error_attempted_exactly_once(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_retries=5, max_vision_pages=5)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_vision=[gemini_server_error()],
            claude_vision=[anthropic_status_error(anthropic.NotFoundError, 404)],
        )

        result = process_file(path, cfg, logger)

        assert recorder.claude_vision_count == 1  # NOT 5 - invalid-model errors are non-transient
        assert result.needs_review is True
        assert result.error is True


# ---------------------------------------------------------------------------
# Part F - call-accounting invariants across routes
# ---------------------------------------------------------------------------

class TestCallAccountingInvariants:
    def test_claude_never_called_on_gemini_success(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        recorder = pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(gt.FIXTURE_01)],
        )
        process_file(path, cfg, logger)
        assert recorder.claude_text_count == 0
        assert recorder.claude_vision_count == 0

    def test_text_pages_never_enter_vision_calls_and_vice_versa(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_03_mixed_text_scan_blank"]
        scenario = gt.FIXTURE_03
        pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_text=[pr.invoice_response_json_subset(scenario, slice(0, 1))],
            gemini_vision=[pr.invoice_response_json_subset(scenario, slice(1, 3))],
        )
        rendered = pr.install_render_spy(monkeypatch)
        process_file(path, cfg, logger)
        rendered_pages = {n for chunk in rendered for n in chunk}
        assert rendered_pages == {3, 4}  # only image pages ever get rendered/sent to vision
        assert 1 not in rendered_pages and 5 not in rendered_pages  # text pages
        assert 2 not in rendered_pages  # blank page

    def test_no_image_page_omitted_or_duplicated_across_chunks(
        self, synthetic_fixture_paths, logger, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        path = synthetic_fixture_paths["fixture_02_multipage_scanned_exceeds_limit"]
        scenario = gt.FIXTURE_02
        responses = [
            pr.invoice_response_json_subset(scenario, slice(0, 2)),
            pr.invoice_response_json_subset(scenario, slice(2, 4)),
            pr.invoice_response_json_subset(scenario, slice(4, 6)),
            pr.invoice_response_json_subset(scenario, slice(6, 7)),
        ]
        pr.install_provider_seams(monkeypatch, cfg, gemini_vision=responses)
        rendered = pr.install_render_spy(monkeypatch)
        process_file(path, cfg, logger)
        flat = [n for chunk in rendered for n in chunk]
        assert sorted(flat) == list(range(1, 8))
        assert len(flat) == len(set(flat))


# ---------------------------------------------------------------------------
# Part G - result and log privacy
# ---------------------------------------------------------------------------

class TestResultAndLogPrivacy:
    def test_result_object_has_no_raw_response_or_secrets(
        self, synthetic_fixture_paths, cfg, logger, monkeypatch
    ):
        path = synthetic_fixture_paths["fixture_01_multipage_text_native"]
        pr.install_provider_seams(
            monkeypatch, cfg, gemini_text=[pr.invoice_response_json(gt.FIXTURE_01)],
        )
        result = process_file(path, cfg, logger)
        # The InvoiceResult/Invoice objects only carry normalized business
        # fields - no raw JSON, no base64, no key material anywhere.
        rendered_repr = repr(result)
        assert cfg.gemini_api_key not in rendered_repr
        assert cfg.anthropic_api_key not in rendered_repr
        assert "base64" not in rendered_repr.lower()

    def test_log_contains_expected_fields_not_secrets_or_raw_text(
        self, synthetic_fixture_paths, tmp_path, monkeypatch
    ):
        # MAX_VISION_PAGES=1 gives two chunks: one that fails on Gemini
        # (exercising the "sanitized error category" log line) and one that
        # succeeds (exercising the "status ok" log line), in the same run.
        cfg = make_config(max_retries=1, max_vision_pages=1)
        path = synthetic_fixture_paths["fixture_09_conflicting_totals"]
        pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_vision=[gemini_server_error(), pr.fixture_09_chunk_response_json([2], "650.00")],
            claude_vision=[anthropic_status_error(anthropic.InternalServerError, 529)],
        )

        log_path = tmp_path / "run.log"
        run_id = new_run_id()
        logger = setup_logging(
            log_path, run_id=run_id,
            secrets=(cfg.gemini_api_key or "", cfg.anthropic_api_key or ""),
        )
        process_file(path, cfg, logger)
        for handler in logger.handlers:
            handler.flush()
        log_text = log_path.read_text()

        # Required present fields
        assert path.name in log_text
        assert "chunk" in log_text  # route/chunk label
        assert "gemini" in log_text or "claude" in log_text  # provider
        assert cfg.gemini_vision_model in log_text or cfg.claude_vision_model in log_text  # model

        # Required absent content
        assert cfg.gemini_api_key not in log_text
        assert cfg.anthropic_api_key not in log_text
        assert "base64" not in log_text.lower()
        # No full extracted page text (the fixture's rendered invoice body)
        assert "Terminal handling" not in log_text
        assert "Demurrage" not in log_text

        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()


# ---------------------------------------------------------------------------
# Part H - directory-level (process_directory) batch test
# ---------------------------------------------------------------------------

class TestDirectoryLevelBatch:
    def test_batch_of_four_completes_with_correct_per_file_results(
        self, tmp_path, cfg, logger, monkeypatch
    ):
        from .synthetic_fixtures import scenarios as sc

        sc.build_scenario("fixture_01_multipage_text_native", tmp_path / "a_text.pdf")
        sc.build_scenario("fixture_09_conflicting_totals", tmp_path / "b_image.pdf")
        (tmp_path / "c_corrupt.pdf").write_bytes(b"garbage, not a pdf")
        sc.build_scenario("fixture_08_repeated_table_headers", tmp_path / "d_provider_fail.pdf")

        cfg = make_config(max_retries=1, max_vision_pages=5)
        pr.install_provider_seams(
            monkeypatch, cfg,
            gemini_text=[
                pr.invoice_response_json(gt.FIXTURE_01),  # a_text.pdf succeeds
                gemini_server_error(),                      # d_provider_fail.pdf fails
            ],
            gemini_vision=[pr.fixture_09_chunk_response_json([0, 1, 2], "650.00")],
            claude_text=[],  # fallback disabled by default - never consulted
        )

        results = process_directory(tmp_path, cfg, logger)

        assert len(results) == 4  # matches discovered PDFs
        by_name = {r.source_file: r for r in results}
        assert set(by_name) == {"a_text.pdf", "b_image.pdf", "c_corrupt.pdf", "d_provider_fail.pdf"}

        assert by_name["a_text.pdf"].error is False
        assert by_name["a_text.pdf"].needs_review is False
        assert by_name["b_image.pdf"].error is False
        assert by_name["b_image.pdf"].document_classification == "image-only"
        assert by_name["c_corrupt.pdf"].error is True
        assert "unreadable PDF" in by_name["c_corrupt.pdf"].review_reason
        assert by_name["d_provider_fail.pdf"].error is True
        # Failures did not stop later files: all four got a result.
