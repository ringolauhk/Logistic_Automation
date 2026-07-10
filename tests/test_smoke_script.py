"""Exit-code policy of the root-level smoke script (test_pipeline.py):
invoice-level review outcomes are NOT program failures; only fatal
tool-level failures exit nonzero.
"""

from pathlib import Path

import openpyxl
import pytest

import test_pipeline as smoke
from invoice_extractor import gemini_client

from .conftest import TEXT_BODY, build_pdf, invoice_json


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ENABLE_CLAUDE_TEXT_FALLBACK", raising=False)


def run(tmp_path, samples: Path) -> tuple[int, Path]:
    out = tmp_path / "out" / "results.xlsx"
    rc = smoke.run_smoke(samples, output_path=out, log_path=tmp_path / "out" / "run.log")
    return rc, out


class TestExpectedOutcomesExitZero:
    def test_no_pdfs_clear_message_exit_zero(self, tmp_path, capsys):
        samples = tmp_path / "samples"
        samples.mkdir()
        rc, _ = run(tmp_path, samples)
        assert rc == 0
        assert "No PDFs found" in capsys.readouterr().out

    def test_missing_keys_review_rows_workbook_written_exit_zero(
        self, tmp_path, capsys
    ):
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "inv.pdf", [("text", TEXT_BODY)])
        rc, out = run(tmp_path, samples)
        assert rc == 0  # missing keys are a provider/config condition, not tool failure
        assert out.exists()
        openpyxl.load_workbook(out).close()
        output = capsys.readouterr().out
        assert "provider/config failures:         1" in output
        assert "needs-review invoices:            1" in output
        assert "not a tool failure - exit 0" in output

    def test_one_provider_failure_batch_continues_exit_zero(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "a_bad.pdf", [("text", TEXT_BODY)])
        build_pdf(samples / "b_good.pdf", [("text", TEXT_BODY)])

        responses = ["not { json at all", invoice_json()]  # a_bad fails, b_good succeeds
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda cfg, model, contents: _pop_or_fail(responses))

        rc, out = run(tmp_path, samples)
        assert rc == 0
        assert out.exists()
        output = capsys.readouterr().out
        assert "PDFs processed:                   2" in output
        assert "successful structured extractions:   1" in output
        assert "provider/config failures:         1" in output


def _pop_or_fail(queue):
    if not queue:
        raise AssertionError("unexpected extra provider call")
    return queue.pop(0)


class TestFatalFailuresExitNonzero:
    def test_missing_input_dir_is_fatal(self, tmp_path, capsys):
        rc = smoke.run_smoke(tmp_path / "does-not-exist",
                             output_path=tmp_path / "o.xlsx",
                             log_path=tmp_path / "run.log")
        assert rc != 0
        assert "FATAL" in capsys.readouterr().out

    def test_workbook_write_failure_is_fatal(self, tmp_path, capsys, monkeypatch):
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "inv.pdf", [("text", TEXT_BODY)])

        def broken_export(results, output_path):
            raise OSError("disk full (synthetic)")

        monkeypatch.setattr(smoke, "export_workbook", broken_export)
        rc, _ = run(tmp_path, samples)
        assert rc != 0
        assert "workbook could not be written" in capsys.readouterr().out

    def test_uncaught_orchestration_failure_is_fatal(self, tmp_path, capsys, monkeypatch):
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "inv.pdf", [("text", TEXT_BODY)])

        def broken_batch(input_dir, cfg, logger):
            raise RuntimeError("orchestrator exploded (synthetic)")

        monkeypatch.setattr(smoke, "process_directory", broken_batch)
        rc, _ = run(tmp_path, samples)
        assert rc != 0
        assert "batch did not complete" in capsys.readouterr().out
