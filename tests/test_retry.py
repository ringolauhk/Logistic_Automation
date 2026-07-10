import anthropic
import httpx
import pytest
from google.genai import errors as genai_errors

from invoice_extractor import claude_client, gemini_client
from invoice_extractor.retry import call_with_retry
from invoice_extractor.schema import ExtractionError


def gemini_error(code):
    return genai_errors.APIError(code, {"error": {"message": "synthetic", "status": "TEST"}})


def anthropic_status_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return cls("synthetic", response=resp, body=None)


class TestGeminiTransience:
    def test_rate_limit_is_transient(self):
        assert gemini_client.is_transient(gemini_error(429))

    def test_server_errors_are_transient(self):
        assert gemini_client.is_transient(gemini_error(500))
        assert gemini_client.is_transient(gemini_error(503))

    def test_auth_error_not_transient(self):
        assert not gemini_client.is_transient(gemini_error(401))
        assert not gemini_client.is_transient(gemini_error(403))

    def test_invalid_model_not_transient(self):
        assert not gemini_client.is_transient(gemini_error(404))

    def test_bad_request_not_transient(self):
        assert not gemini_client.is_transient(gemini_error(400))

    def test_network_and_timeout_are_transient(self):
        assert gemini_client.is_transient(httpx.ConnectError("boom"))
        assert gemini_client.is_transient(httpx.ReadTimeout("slow"))
        assert gemini_client.is_transient(ConnectionError("reset"))
        assert gemini_client.is_transient(TimeoutError("late"))

    def test_extraction_error_not_transient(self):
        assert not gemini_client.is_transient(ExtractionError("bad output"))


class TestClaudeTransience:
    def test_rate_limit_is_transient(self):
        assert claude_client.is_transient(
            anthropic_status_error(anthropic.RateLimitError, 429))

    def test_server_error_is_transient(self):
        assert claude_client.is_transient(
            anthropic_status_error(anthropic.InternalServerError, 529))

    def test_timeout_is_transient(self):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        assert claude_client.is_transient(anthropic.APITimeoutError(request=req))

    def test_auth_error_not_transient(self):
        assert not claude_client.is_transient(
            anthropic_status_error(anthropic.AuthenticationError, 401))

    def test_invalid_model_not_transient(self):
        assert not claude_client.is_transient(
            anthropic_status_error(anthropic.NotFoundError, 404))


class TestCallWithRetry:
    def is_conn_error(self, exc):
        return isinstance(exc, ConnectionError)

    def test_transient_errors_retried_until_success(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("flaky")
            return "ok"

        result, attempts = call_with_retry(fn, self.is_conn_error, 3, "test")
        assert result == "ok"
        assert attempts == 3 and len(calls) == 3

    def test_attempts_bounded_then_reraise(self):
        calls = []

        def fn():
            calls.append(1)
            raise ConnectionError("always down")

        with pytest.raises(ConnectionError):
            call_with_retry(fn, self.is_conn_error, 3, "test")
        assert len(calls) == 3  # exactly max attempts, no more

    def test_non_transient_raises_immediately(self):
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("bad request - retrying would repeat the failure")

        with pytest.raises(ValueError):
            call_with_retry(fn, self.is_conn_error, 5, "test")
        assert len(calls) == 1

    def test_success_on_first_attempt(self):
        result, attempts = call_with_retry(lambda: "fast", self.is_conn_error, 3, "test")
        assert result == "fast" and attempts == 1
