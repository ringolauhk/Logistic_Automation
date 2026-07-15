"""M6 benchmark CLI: threshold exit codes (AL-AN) and a synthetic end-to-end
benchmark spanning text/vision/mixed/review/failed cases (AV). Plus the
opt-in --run-metadata flag on `run`. All offline."""

import json
from pathlib import Path

import openpyxl
import pytest
from click.testing import CliRunner

from invoice_extractor import gemini_client, openrouter_client
from invoice_extractor.cli import cli

from .benchmark_helpers import (
    gt,
    invoice_row,
    line_row,
    manifest_entry,
    write_manifest,
    write_workbook,
)
from .conftest import TEXT_BODY, build_pdf, invoice_json


def _env(monkeypatch, **extra):
    for var in ("LLM_GATEWAY", "OPENROUTER_API_KEY", "OPENROUTER_TEXT_MODELS",
                "OPENROUTER_VISION_MODELS", "MAX_TEXT_PAGES", "MAX_VISION_PAGES",
                "MAX_RETRIES", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                "MAX_MODEL_ATTEMPTS_PER_FILE", "MAX_COST_USD_PER_FILE",
                "MAX_COST_USD_PER_RUN"):
        monkeypatch.delenv(var, raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _envelope(content, **usage):
    u = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600,
         "cost": 0.0002, "completion_tokens_details": {"reasoning_tokens": 5}}
    u.update(usage)
    return {"id": "gen-1", "model": "served-m",
            "choices": [{"finish_reason": "stop", "native_finish_reason": "STOP",
                         "message": {"content": content}}], "usage": u}


# --- AL/AM/AN: thresholds -----------------------------------------------------

def _perfect_dataset(tmp_path):
    manifest = write_manifest(
        tmp_path, [manifest_entry("c1", "a.pdf")],
        {"c1": gt("c1", invoice={"invoice_number": "INV-1", "total_amount": "10.00"},
                  line_items=[{"line_no": "1", "amount": "10.00"}])},
    )
    wb = write_workbook(
        tmp_path / "results.xlsx",
        [invoice_row("INV-1", "a.pdf", invoice_number="INV-1", total_amount=10.0)],
        [line_row("INV-1", "a.pdf", 1, line_no="1", amount=10.0)],
    )
    return manifest, wb


def test_al_threshold_pass_exit_zero(tmp_path):
    manifest, wb = _perfect_dataset(tmp_path)
    thresholds = tmp_path / "th.json"
    thresholds.write_text(json.dumps({"minimum_line_recall": "0.90"}), encoding="utf-8")
    result = CliRunner().invoke(cli, [
        "benchmark", "score", "--manifest", str(manifest), "--workbook", str(wb),
        "--thresholds", str(thresholds), "--output", str(tmp_path / "b.xlsx")])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_am_threshold_failure_exit_nonzero(tmp_path):
    manifest = write_manifest(
        tmp_path, [manifest_entry("c1", "a.pdf")],
        {"c1": gt("c1", line_items=[{"line_no": "1", "amount": "10.00"},
                                    {"line_no": "2", "amount": "20.00"}])})
    wb = write_workbook(tmp_path / "results.xlsx", [invoice_row("INV-1", "a.pdf")],
                        [line_row("INV-1", "a.pdf", 1, line_no="1", amount=10.0)])
    thresholds = tmp_path / "th.json"
    thresholds.write_text(json.dumps({"minimum_line_recall": "0.90"}), encoding="utf-8")
    result = CliRunner().invoke(cli, [
        "benchmark", "score", "--manifest", str(manifest), "--workbook", str(wb),
        "--thresholds", str(thresholds), "--output", str(tmp_path / "b.xlsx")])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_an_no_thresholds_exit_zero(tmp_path):
    manifest, wb = _perfect_dataset(tmp_path)
    result = CliRunner().invoke(cli, [
        "benchmark", "score", "--manifest", str(manifest), "--workbook", str(wb),
        "--output", str(tmp_path / "b.xlsx")])
    assert result.exit_code == 0, result.output


def test_config_error_exit_two(tmp_path):
    # Manifest referencing a missing GT file -> benchmark config error -> exit 2.
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [
        {"case_id": "c1", "source_file": "a.pdf", "document_type": "text_single_page",
         "expected_outcome": "extracted", "ground_truth": "ground_truth/missing.json"}]}),
        encoding="utf-8")
    wb = write_workbook(tmp_path / "results.xlsx", [invoice_row("INV-1", "a.pdf")], [])
    result = CliRunner().invoke(cli, [
        "benchmark", "score", "--manifest", str(manifest), "--workbook", str(wb),
        "--output", str(tmp_path / "b.xlsx")])
    assert result.exit_code == 2
    assert "BENCHMARK CONFIG ERROR" in result.output


def test_fuzzy_flag_recorded_in_summary(tmp_path):
    manifest, wb = _perfect_dataset(tmp_path)
    out = tmp_path / "b.xlsx"
    result = CliRunner().invoke(cli, [
        "benchmark", "score", "--manifest", str(manifest), "--workbook", str(wb),
        "--enable-fuzzy-line-matching", "--output", str(out)])
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "b.json").read_text())
    assert payload["fuzzy_enabled"] is True


# --- run --run-metadata is strictly opt-in ------------------------------------

class TestRunMetadataOptIn:
    def _samples(self, tmp_path):
        s = tmp_path / "samples"
        s.mkdir()
        build_pdf(s / "inv.pdf", [("text", TEXT_BODY)])
        return s

    def test_no_flag_writes_no_sidecar(self, tmp_path, monkeypatch):
        _env(monkeypatch, GEMINI_API_KEY="k")
        monkeypatch.setattr(gemini_client, "_generate", lambda c, m, ct: invoice_json())
        out = tmp_path / "out" / "results.xlsx"
        result = CliRunner().invoke(cli, ["run", "--input", str(self._samples(tmp_path)),
                                          "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "out" / "results.run.json").exists()
        assert list((tmp_path / "out").glob("*.run.json")) == []

    def test_flag_writes_safe_metadata_only(self, tmp_path, monkeypatch):
        _env(monkeypatch, GEMINI_API_KEY="k")
        monkeypatch.setattr(gemini_client, "_generate", lambda c, m, ct: invoice_json())
        out = tmp_path / "out" / "results.xlsx"
        meta = tmp_path / "out" / "results.run.json"
        result = CliRunner().invoke(cli, ["run", "--input", str(self._samples(tmp_path)),
                                          "--output", str(out), "--run-metadata", str(meta)])
        assert result.exit_code == 0, result.output
        payload = json.loads(meta.read_text())
        # M7 extended the sidecar with operational status (still no invoice content).
        assert set(payload) == {"run_id", "started_at", "finished_at", "interrupted",
                                "exit_code", "input_dir", "output_artifacts", "files"}
        assert payload["interrupted"] is False
        entry = payload["files"][0]
        assert set(entry) == {"source_file", "elapsed_seconds", "extraction_method",
                              "provider", "model", "needs_review", "error", "completed",
                              "interrupted", "request_count", "reported_cost",
                              "unknown_cost_count"}
        # No invoice content / review reasons / prompts / responses persisted.
        blob = meta.read_text()
        for forbidden in ("review_reason", "INV-1001", "Ocean freight", "prompt", "Acme"):
            assert forbidden not in blob


# --- AV: synthetic end-to-end benchmark (extract, then score) -----------------

class TestSyntheticEndToEnd:
    def test_av_full_benchmark_over_heterogeneous_batch(self, tmp_path, monkeypatch):
        _env(monkeypatch, LLM_GATEWAY="openrouter", OPENROUTER_API_KEY="test-or-key",
             OPENROUTER_TEXT_MODELS="tv/text-1", OPENROUTER_VISION_MODELS="tv/vis-1",
             MAX_TEXT_PAGES="2", MAX_VISION_PAGES="2", MAX_RETRIES="1")
        samples = tmp_path / "samples"
        samples.mkdir()
        build_pdf(samples / "text.pdf", [("text", TEXT_BODY)])
        build_pdf(samples / "scan.pdf", [("image",)])
        build_pdf(samples / "mixed.pdf", [("text", TEXT_BODY), ("image",)])
        build_pdf(samples / "review.pdf", [("text", TEXT_BODY)])
        (samples / "failed.pdf").write_bytes(b"NOT-A-PDF")

        # process_directory sorts filenames; call order is therefore:
        # mixed(text chunk, vision chunk), review, scan, text = 5 calls
        # (failed.pdf is unreadable and makes no call).
        def item(desc, amt):
            return {"description": desc, "quantity": 1, "unit_price": amt, "amount": amt}

        def full(desc, amt, total, **extra):
            return json.dumps({"invoice_number": "INV-1", "invoice_date": "2026-07-01",
                               "currency": "USD", "seller_name": "Acme",
                               "total_amount": total, "line_items": [item(desc, amt)], **extra})
        responses = [
            _envelope(json.dumps({"line_items": [item("Mixed text line", 10)]})),   # mixed text
            _envelope(full("Mixed vision line", 20, 30)),     # mixed vision (total 30 = 10+20)
            _envelope(full("Review line", 5, 999)),           # review.pdf: totals inconclusive
            _envelope(full("Scan line", 50, 50)),             # scan.pdf
            _envelope(full("Text line", 100, 100)),           # text.pdf
        ]
        rec = _Recorder(responses)
        monkeypatch.setattr(openrouter_client, "_chat_completion", rec)
        no_direct = lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct called"))
        monkeypatch.setattr(gemini_client, "_generate", no_direct)

        out = tmp_path / "out" / "results.xlsx"
        run_result = CliRunner().invoke(cli, ["run", "--input", str(samples),
                                              "--output", str(out)])
        assert run_result.exit_code == 0, run_result.output
        usage = out.parent / "results.usage.csv"
        assert usage.exists()

        # Build ground truth for the five cases and score.
        cases = [
            manifest_entry("text", "text.pdf", "text_single_page", "extracted"),
            manifest_entry("scan", "scan.pdf", "vision_single_page", "extracted"),
            manifest_entry("mixed", "mixed.pdf", "mixed", "extracted"),
            manifest_entry("review", "review.pdf", "text_single_page", "needs_review"),
            manifest_entry("failed", "failed.pdf", "malformed", "failed"),
        ]
        gts = {
            "text": gt("text", invoice={"invoice_number": "INV-1", "currency": "USD",
                                        "total_amount": "100.00"},
                       line_items=[{"description": "Text line", "amount": "100.00"}]),
            "scan": gt("scan", invoice={"invoice_number": "INV-1", "total_amount": "50.00"},
                       line_items=[{"description": "Scan line", "amount": "50.00"}]),
            "mixed": gt("mixed", invoice={"invoice_number": "INV-1", "total_amount": "30.00"},
                        line_items=[{"description": "Mixed text line", "amount": "10.00"},
                                    {"description": "Mixed vision line", "amount": "20.00"}]),
            "review": gt("review", invoice={"total_amount": "999.00"},
                         line_items=[{"description": "Review line", "amount": "5.00"}],
                         expected_needs_review=True,
                         accepted_review_categories=["totals_inconclusive"]),
            "failed": gt("failed", expected_needs_review=True),
        }
        manifest = write_manifest(tmp_path, cases, gts)
        report_out = tmp_path / "bench.xlsx"
        score_result = CliRunner().invoke(cli, [
            "benchmark", "score", "--manifest", str(manifest), "--workbook", str(out),
            "--usage", str(usage), "--output", str(report_out)])
        assert score_result.exit_code == 0, score_result.output

        wb = openpyxl.load_workbook(report_out)
        assert len(wb.sheetnames) == 8
        payload = json.loads((tmp_path / "bench.json").read_text())
        a = payload["aggregates"]
        assert a["num_cases"] == 5
        # text/scan/mixed extracted; review is a needs_review TP.
        by_id = {c["case_id"]: c for c in payload["cases"]}
        assert by_id["text"]["actual_outcome"] == "extracted"
        assert by_id["review"]["review_class"] == "TP"
        assert by_id["failed"]["invoice_status"] == "matched"
        assert by_id["failed"]["actual_outcome"] == "failed"
        # Usage CSV carried both routes for the mixed case.
        assert set(by_id["mixed"]["routes"]) == {"text", "vision"}


class _Recorder:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, cfg, *, model, messages, response_format=None, max_tokens, timeout=None):
        if not self.responses:
            raise AssertionError("provider called more than expected")
        return self.responses.pop(0)
