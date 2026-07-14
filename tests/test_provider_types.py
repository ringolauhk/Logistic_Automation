"""Provider boundary types (M1): safety and shape of ProviderRequest,
ProviderResult, ProviderError, and the small helpers."""

from decimal import Decimal

from invoice_extractor.logging_setup import exc_summary
from invoice_extractor.provider import (
    ATTEMPT_PRIMARY,
    MODE_PROMPT_ONLY,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    coerce_cost,
    is_truncated,
)
from invoice_extractor.schema import ExtractionError


class TestReprSafety:
    def test_provider_request_repr_hides_messages(self):
        req = ProviderRequest(
            route="vision", requested_model="vendor/m", structured_mode=MODE_PROMPT_ONLY,
            max_tokens=8192,
            messages=[{"role": "user", "content": "INVOICE-SECRET-BODY-123 and base64 AAAA"}],
        )
        text = repr(req)
        assert "INVOICE-SECRET-BODY-123" not in text
        assert "AAAA" not in text
        # non-sensitive fields still visible for debugging
        assert "vendor/m" in text and "vision" in text

    def test_provider_result_repr_hides_output_text(self):
        res = ProviderResult(
            requested_model="vendor/m", route="text",
            text='{"seller_name": "CONFIDENTIAL-SELLER-XYZ"}',
            actual_model="vendor/actual", cost_usd=Decimal("0.001"),
        )
        text = repr(res)
        assert "CONFIDENTIAL-SELLER-XYZ" not in text
        assert "vendor/actual" in text  # provenance still visible

    def test_no_api_key_or_raw_dict_fields_exist(self):
        # The types must not even have slots/attrs for keys or raw responses.
        res = ProviderResult(requested_model="m", route="text")
        for forbidden in ("api_key", "authorization", "headers", "raw", "raw_response", "response"):
            assert not hasattr(res, forbidden)
        req = ProviderRequest(route="text", requested_model="m",
                              structured_mode=MODE_PROMPT_ONLY, max_tokens=1)
        for forbidden in ("api_key", "authorization", "headers"):
            assert not hasattr(req, forbidden)


class TestProviderResultFields:
    def test_has_all_required_provenance_and_usage_fields(self):
        res = ProviderResult(requested_model="m", route="text")
        for f in ("gateway", "requested_model", "actual_model", "route", "attempt_type",
                  "attempt_index", "structured_mode", "input_tokens", "output_tokens",
                  "reasoning_tokens", "total_tokens", "cost_usd", "finish_reason",
                  "native_finish_reason", "generation_id", "latency_ms"):
            assert hasattr(res, f), f
        assert res.gateway == "openrouter"
        assert res.attempt_type == ATTEMPT_PRIMARY

    def test_is_truncated(self):
        assert is_truncated(ProviderResult(requested_model="m", route="text",
                                           finish_reason="length")) is True
        assert is_truncated(ProviderResult(requested_model="m", route="text",
                                           finish_reason="stop")) is False


class TestCoerceCost:
    def test_number_becomes_decimal(self):
        assert coerce_cost(0.00042) == Decimal("0.00042")
        assert isinstance(coerce_cost(0.00042), Decimal)

    def test_string_number_ok(self):
        assert coerce_cost("0.01") == Decimal("0.01")

    def test_none_and_garbage_become_none(self):
        assert coerce_cost(None) is None
        assert coerce_cost("not-a-number") is None


class TestProviderError:
    def test_is_extraction_error_subclass(self):
        exc = ProviderError("OpenRouter request failed (HTTP 429)",
                            category="rate_limited", http_status=429)
        assert isinstance(exc, ExtractionError)
        assert exc.category == "rate_limited"
        assert exc.http_status == 429

    def test_message_is_trusted_and_safe_via_exc_summary(self):
        # Subclasses ExtractionError, so exc_summary includes its (safe) message.
        exc = ProviderError("OpenRouter request failed (HTTP 503)",
                            category="server_error", http_status=503)
        summary = exc_summary(exc)
        assert "ProviderError" in summary
        assert "HTTP 503" in summary
