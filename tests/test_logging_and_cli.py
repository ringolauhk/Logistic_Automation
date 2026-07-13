import logging

import openpyxl
import pandas as pd
from click.testing import CliRunner

from invoice_extractor import claude_client, gemini_client
from invoice_extractor import cli as cli_module
from invoice_extractor.cli import cli
from invoice_extractor.logging_setup import exc_summary, new_run_id, setup_logging

from .conftest import TEXT_BODY, build_pdf, invoice_json


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

    def test_doctor_shows_provider_roles_and_key_status_no_secrets(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gem-SHOULD-NOT-PRINT")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()

        result = CliRunner().invoke(
            cli, ["doctor", "--input", str(samples), "--output", str(tmp_path / "out")]
        )

        assert result.exit_code == 0, result.output
        assert "GEMINI_API_KEY" in result.output and "set" in result.output
        assert "ANTHROPIC_API_KEY" in result.output and "NOT SET" in result.output
        assert "sk-gem-SHOULD-NOT-PRINT" not in result.output
        # fixed provider roles are stated explicitly, not left implicit
        assert "Provider roles" in result.output
        assert "fallback" in result.output.lower()
        assert "text route" in result.output and "vision route" in result.output


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


class TestDoctorLiveFiltering:
    """--provider/--route restrict which of the 4 probes run.

    All provider calls are mocked - these tests make no network calls and
    only need fake key env vars to get past the `gem`/`claude` presence gate.
    """

    def _invoke(self, tmp_path, monkeypatch, extra_args, gen_ret="OK", req_ret="OK"):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
        monkeypatch.setattr(gemini_client, "_generate", lambda cfg, model, contents: gen_ret)
        monkeypatch.setattr(claude_client, "_request", lambda cfg, model, content: req_ret)
        samples = tmp_path / "samples"
        samples.mkdir()
        return CliRunner().invoke(
            cli, ["doctor", "--input", str(samples), "--output", str(tmp_path / "out"),
                  "--live", *extra_args],
        )

    def test_default_runs_all_four_probes(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, [])
        assert result.exit_code == 0, result.output
        for label in ("gemini text", "gemini vision", "claude text", "claude vision"):
            assert label in result.output

    def test_provider_gemini_only(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--provider", "gemini"])
        assert result.exit_code == 0, result.output
        assert "gemini text" in result.output and "gemini vision" in result.output
        assert "claude text" not in result.output and "claude vision" not in result.output

    def test_provider_claude_only(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--provider", "claude"])
        assert result.exit_code == 0, result.output
        assert "claude text" in result.output and "claude vision" in result.output
        assert "gemini text" not in result.output and "gemini vision" not in result.output

    def test_route_text_only(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--route", "text"])
        assert result.exit_code == 0, result.output
        assert "gemini text" in result.output and "claude text" in result.output
        assert "gemini vision" not in result.output and "claude vision" not in result.output

    def test_route_vision_only(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--route", "vision"])
        assert result.exit_code == 0, result.output
        assert "gemini vision" in result.output and "claude vision" in result.output
        assert "gemini text" not in result.output and "claude text" not in result.output

    def test_single_provider_and_route_combination(self, tmp_path, monkeypatch):
        result = self._invoke(
            tmp_path, monkeypatch, ["--provider", "gemini", "--route", "vision"]
        )
        assert result.exit_code == 0, result.output
        assert "gemini vision" in result.output
        for label in ("gemini text", "claude text", "claude vision"):
            assert label not in result.output

    def test_invalid_provider_rejected(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--provider", "bogus"])
        assert result.exit_code != 0

    def test_invalid_route_rejected(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, ["--route", "bogus"])
        assert result.exit_code != 0


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


class TestRunCommand:
    """The real operator entrypoint (`python -m invoice_extractor run`), as
    opposed to the parallel root-level smoke script exercised by
    test_smoke_script.py - same exit-code policy, but this is what ships."""

    def _samples_with_one_pdf(self, tmp_path, monkeypatch, name="inv.pdf"):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / name, [("text", TEXT_BODY)])
        return samples

    def test_writes_workbook_and_prints_summary_counts(self, tmp_path, monkeypatch):
        samples = self._samples_with_one_pdf(tmp_path, monkeypatch)
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: invoice_json())
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert result.exit_code == 0, result.output
        assert output.exists()
        wb = openpyxl.load_workbook(output)
        assert wb.sheetnames == ["Invoices", "LineItems", "NeedsReview"]  # unchanged contract
        assert "Files processed:      1" in result.output
        assert "Invoices extracted:   1" in result.output
        assert "Line items extracted: 1" in result.output
        assert "Needs review:         0" in result.output
        assert "Failed/problem:       0" in result.output

    def test_exit_zero_when_invoice_needs_review(self, tmp_path, monkeypatch):
        samples = self._samples_with_one_pdf(tmp_path, monkeypatch)
        # total_amount doesn't reconcile with the line items -> flagged, not fatal
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: invoice_json(total_amount=999.0))
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert result.exit_code == 0, result.output
        assert "Needs review:         1" in result.output
        review = pd.read_excel(output, sheet_name="NeedsReview")
        assert len(review) == 1

    def test_exit_zero_when_one_file_fails_batch_continues(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "a_bad.pdf", [("text", TEXT_BODY)])
        build_pdf(samples / "b_good.pdf", [("text", TEXT_BODY)])
        responses = ["not { json at all", invoice_json()]  # a_bad fails, b_good succeeds
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: responses.pop(0))
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert result.exit_code == 0, result.output  # one bad file never stops the batch
        assert output.exists()
        assert "Files processed:      2" in result.output
        assert "Failed/problem:       1" in result.output

    def test_missing_gemini_key_is_clean_review_not_crash(self, tmp_path, monkeypatch):
        # No key configured at all, and gemini_client._generate is NOT
        # mocked - this exercises the real _get_client() RuntimeError guard
        # end to end. Missing a provider key is a review outcome, not a CLI
        # failure (see README "Review outcomes vs program failure").
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "inv.pdf", [("text", TEXT_BODY)])
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output
        assert output.exists()
        assert "Needs review:         1" in result.output
        assert "Failed/problem:       1" in result.output
        review = pd.read_excel(output, sheet_name="NeedsReview")
        assert "GEMINI_API_KEY" in review.iloc[0]["review_reason"]

    def test_missing_input_folder_exits_nonzero(self, tmp_path):
        result = CliRunner().invoke(
            cli, ["run", "--input", str(tmp_path / "does-not-exist")]
        )
        assert result.exit_code != 0

    def test_no_pdfs_found_exits_zero(self, tmp_path):
        samples = tmp_path / "samples"
        samples.mkdir()
        result = CliRunner().invoke(cli, ["run", "--input", str(samples)])
        assert result.exit_code == 0, result.output
        assert "No PDFs found" in result.output

    def test_summary_includes_output_path(self, tmp_path, monkeypatch):
        samples = self._samples_with_one_pdf(tmp_path, monkeypatch)
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: invoice_json())
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert str(output) in result.output

    def test_no_secrets_printed_in_normal_output(self, tmp_path, monkeypatch):
        samples = self._samples_with_one_pdf(tmp_path, monkeypatch)
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gem-SHOULD-NOT-PRINT")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SHOULD-NOT-PRINT")
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: invoice_json())
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert "sk-gem-SHOULD-NOT-PRINT" not in result.output
        assert "sk-ant-SHOULD-NOT-PRINT" not in result.output

    def test_workbook_write_failure_exits_nonzero(self, tmp_path, monkeypatch):
        samples = self._samples_with_one_pdf(tmp_path, monkeypatch)
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: invoice_json())

        def broken_export(results, output_path):
            raise OSError("disk full (synthetic)")

        monkeypatch.setattr(cli_module, "export_workbook", broken_export)
        output = tmp_path / "out" / "results.xlsx"

        result = CliRunner().invoke(
            cli, ["run", "--input", str(samples), "--output", str(output)]
        )

        assert result.exit_code != 0
        assert "workbook could not be written" in result.output
