import logging

from click.testing import CliRunner

from invoice_extractor.cli import cli
from invoice_extractor.logging_setup import exc_summary, new_run_id, setup_logging


class TestLogging:
    def test_run_id_in_every_line_and_secrets_redacted(self, tmp_path):
        log_path = tmp_path / "run.log"
        run_id = new_run_id()
        logger = setup_logging(log_path, run_id=run_id,
                               secrets=("sk-SECRET-KEY-123",), verbose=False)
        logger.info("starting with key sk-SECRET-KEY-123 configured")
        logger.warning("plain line")
        for handler in logger.handlers:
            handler.flush()
        content = log_path.read_text()
        assert "sk-SECRET-KEY-123" not in content
        assert "***REDACTED***" in content
        assert content.count(f"[{run_id}]") == 2
        # cleanup handlers so later tests/file locks are unaffected
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()

    def test_exc_summary_truncates_long_messages(self):
        exc = ValueError("x" * 5000)
        summary = exc_summary(exc)
        assert len(summary) < 300
        assert summary.startswith("ValueError:")
        assert "[truncated]" in summary

    def test_exc_summary_flattens_newlines(self):
        summary = exc_summary(RuntimeError("line1\nline2\nline3"))
        assert "\n" not in summary


class TestDoctorOffline:
    def test_doctor_runs_offline_without_keys(self, tmp_path, monkeypatch):
        for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()
        result = CliRunner().invoke(
            cli, ["doctor", "--input", str(samples), "--output", str(tmp_path / "out")]
        )
        assert result.exit_code == 0, result.output
        assert "GEMINI_API_KEY" in result.output and "NOT SET" in result.output
        assert "gemini_text" in result.output  # model names shown
        assert "offline mode" in result.output  # no live probes without --live
        # keys must never be printed even when set
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gem-VALUE-SHOULD-NOT-PRINT")
        result = CliRunner().invoke(
            cli, ["doctor", "--input", str(samples), "--output", str(tmp_path / "out")]
        )
        assert "sk-gem-VALUE-SHOULD-NOT-PRINT" not in result.output


class TestProbeErrorClassification:
    def test_categories(self):
        import anthropic
        import httpx
        from google.genai import errors as genai_errors

        from invoice_extractor.cli import classify_probe_error

        def gem(code):
            return genai_errors.APIError(code, {"error": {"message": "x"}})

        def claude(cls, status):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            return cls("x", response=httpx.Response(status, request=req), body=None)

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [
            (RuntimeError("GEMINI_API_KEY is not set"), "missing key"),
            (gem(401), "authentication failure"),
            (gem(403), "authentication failure"),
            (gem(404), "model not found or unavailable"),
            (gem(429), "rate limited"),
            (gem(503), "provider server error"),
            (claude(anthropic.AuthenticationError, 401), "authentication failure"),
            (claude(anthropic.NotFoundError, 404), "model not found or unavailable"),
            (claude(anthropic.RateLimitError, 429), "rate limited"),
            (anthropic.APITimeoutError(request=req), "timeout"),
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("refused"), "network failure"),
        ]
        for exc, expected in cases:
            assert classify_probe_error(exc) == expected, (type(exc).__name__, expected)


class TestClassifyCommand:
    def test_classify_reports_page_level(self, tmp_path, pdf_factory, monkeypatch):
        from .conftest import TEXT_BODY, build_pdf

        build_pdf(tmp_path / "mix.pdf", [("text", TEXT_BODY), ("image",), ("blank",)])
        result = CliRunner().invoke(cli, ["classify", "--input", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "mixed" in result.output
        assert "page 1: text" in result.output
        assert "page 2: image" in result.output
        assert "page 3: blank" in result.output
