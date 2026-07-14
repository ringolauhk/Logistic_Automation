"""Unit tests for invoice_extractor.usage: UsageRecord construction, the CSV
sidecar writer, and the end-of-run summary. Pure/offline - no provider
mocking needed at this level."""

import csv
from decimal import Decimal

from invoice_extractor.provider import ProviderResult
from invoice_extractor.usage import (
    USAGE_CSV_COLUMNS,
    LadderExhaustedError,
    format_usage_summary,
    summarize_usage,
    usage_csv_path,
    usage_record_for_failed_attempt,
    usage_record_from_result,
    write_usage_csv,
)


def _accepted_record(model="vendor/a", cost="0.001", ladder_index=0, attempt_type="primary"):
    result = ProviderResult(
        requested_model=model, route="text", actual_model=f"{model}-served",
        attempt_type=attempt_type, structured_mode="json_schema",
        input_tokens=100, output_tokens=50, reasoning_tokens=5, total_tokens=150,
        cost_usd=Decimal(cost) if cost is not None else None,
        finish_reason="stop", native_finish_reason="STOP", generation_id="gen-1",
        latency_ms=123.4,
    )
    return usage_record_from_result(
        result, run_id="run-1", source_file="inv.pdf", page_range="1-3",
        ladder_index=ladder_index, accepted=True,
    )


class TestUsageRecordFromResult:
    def test_fields_map_from_provider_result(self):
        r = _accepted_record()
        assert r.run_id == "run-1"
        assert r.source_file == "inv.pdf"
        assert r.route == "text"
        assert r.page_range == "1-3"
        assert r.requested_model == "vendor/a"
        assert r.actual_model == "vendor/a-served"
        assert r.input_tokens == 100 and r.output_tokens == 50
        assert r.reasoning_tokens == 5 and r.total_tokens == 150
        assert r.cost_usd == Decimal("0.001")
        assert r.finish_reason == "stop" and r.native_finish_reason == "STOP"
        assert r.generation_id == "gen-1"
        assert r.accepted is True
        assert r.rejection_category is None

    def test_rejected_result_carries_category(self):
        result = ProviderResult(requested_model="vendor/a", route="text")
        r = usage_record_from_result(
            result, run_id="run-1", source_file="inv.pdf", page_range="1",
            ladder_index=0, accepted=False, rejection_category="malformed_json",
        )
        assert r.accepted is False
        assert r.rejection_category == "malformed_json"


class TestUsageRecordForFailedAttempt:
    def test_all_content_fields_are_none(self):
        r = usage_record_for_failed_attempt(
            run_id="run-1", source_file="inv.pdf", route="text", page_range="1",
            attempt_type="primary", ladder_index=0, requested_model="vendor/a",
            structured_mode="json_schema", rejection_category="rate_limited",
            http_status=429,
        )
        assert r.accepted is False
        assert r.rejection_category == "rate_limited"
        assert r.http_status == 429
        for field in ("actual_model", "input_tokens", "output_tokens",
                      "reasoning_tokens", "total_tokens", "cost_usd",
                      "finish_reason", "native_finish_reason", "generation_id",
                      "latency_ms"):
            assert getattr(r, field) is None


class TestLadderExhaustedError:
    def test_carries_usage_records(self):
        records = [_accepted_record()]
        exc = LadderExhaustedError("vendor/a: HTTP 429", records)
        assert exc.usage_records is records
        assert str(exc) == "vendor/a: HTTP 429"

    def test_is_an_extraction_error(self):
        from invoice_extractor.schema import ExtractionError
        assert isinstance(LadderExhaustedError("x", []), ExtractionError)


class TestWriteUsageCsv:
    def test_deterministic_column_order(self, tmp_path):
        path = write_usage_csv([_accepted_record()], tmp_path / "out.usage.csv")
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == USAGE_CSV_COLUMNS

    def test_one_row_per_record(self, tmp_path):
        records = [_accepted_record(model="vendor/a"), _accepted_record(model="vendor/b")]
        path = write_usage_csv(records, tmp_path / "out.usage.csv")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3  # header + 2 records

    def test_cost_serialized_as_decimal_string_not_float_repr(self, tmp_path):
        r = _accepted_record(cost="0.00012345")
        path = write_usage_csv([r], tmp_path / "out.usage.csv")
        content = path.read_text()
        assert "0.00012345" in content
        assert "1.2345e-05" not in content  # never scientific/float notation

    def test_missing_cost_is_blank_not_zero(self, tmp_path):
        r = _accepted_record(cost=None)
        path = write_usage_csv([r], tmp_path / "out.usage.csv")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        cost_idx = USAGE_CSV_COLUMNS.index("cost_usd")
        assert rows[1][cost_idx] == ""  # blank, distinguishable from "0"

    def test_empty_records_still_writes_header_only(self, tmp_path):
        path = write_usage_csv([], tmp_path / "out.usage.csv")
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [USAGE_CSV_COLUMNS]

    def test_utf8_and_deterministic_filename(self, tmp_path):
        path = usage_csv_path(tmp_path / "output" / "results.xlsx")
        assert path == tmp_path / "output" / "results.usage.csv"

    def test_no_confidential_content_possible(self, tmp_path):
        # UsageRecord structurally has no field that could carry invoice
        # text/prompts/images - confirm the written CSV only ever contains
        # the declared metadata columns' values.
        r = _accepted_record()
        path = write_usage_csv([r], tmp_path / "out.usage.csv")
        content = path.read_text()
        for forbidden in ("base64", "prompt", "Authorization", "Bearer"):
            assert forbidden.lower() not in content.lower()


class TestSummarizeUsage:
    def test_exact_decimal_cost_sum(self):
        records = [
            _accepted_record(cost="0.0001", model="vendor/a"),
            _accepted_record(cost="0.0002", model="vendor/a", attempt_type="repair"),
            _accepted_record(cost="0.0003", model="vendor/b", ladder_index=1,
                            attempt_type="escalation"),
        ]
        s = summarize_usage(records)
        assert s.total_cost_usd == Decimal("0.0006")
        assert isinstance(s.total_cost_usd, Decimal)

    def test_missing_cost_contributes_zero_not_none(self):
        records = [_accepted_record(cost=None), _accepted_record(cost="0.001")]
        s = summarize_usage(records)
        assert s.total_cost_usd == Decimal("0.001")

    def test_attempt_type_counts(self):
        records = [
            _accepted_record(attempt_type="primary"),
            _accepted_record(attempt_type="repair"),
            _accepted_record(attempt_type="escalation"),
            _accepted_record(attempt_type="escalation"),
        ]
        s = summarize_usage(records)
        assert s.primary_requests == 1
        assert s.repair_requests == 1
        assert s.escalation_requests == 2
        assert s.total_requests == 4

    def test_cost_by_model_and_accepted_by_model(self):
        records = [
            _accepted_record(model="vendor/a", cost="0.001"),
            _accepted_record(model="vendor/a", cost="0.002"),
            _accepted_record(model="vendor/b", cost="0.005"),
        ]
        s = summarize_usage(records)
        assert s.cost_by_model["vendor/a"] == Decimal("0.003")
        assert s.cost_by_model["vendor/b"] == Decimal("0.005")
        assert s.accepted_by_model["vendor/a"] == 2
        assert s.accepted_by_model["vendor/b"] == 1

    def test_accepted_vs_rejected_counts(self):
        result = ProviderResult(requested_model="vendor/a", route="text")
        rejected = usage_record_from_result(
            result, run_id="r", source_file="f", page_range="1",
            ladder_index=0, accepted=False, rejection_category="malformed_json",
        )
        s = summarize_usage([_accepted_record(), rejected])
        assert s.accepted_requests == 1
        assert s.rejected_requests == 1

    # --- F: unknown cost is counted, not silently folded into zero ----------

    def test_f_unknown_cost_count_reflects_none_cost_records(self):
        records = [
            _accepted_record(cost=None),
            _accepted_record(cost=None),
            _accepted_record(cost="0.001"),
        ]
        s = summarize_usage(records)
        assert s.unknown_cost_count == 2
        assert s.total_cost_usd == Decimal("0.001")  # unknown still contributes $0

    # --- G: no unknown costs leaves the count at zero ------------------------

    def test_g_no_unknown_costs_gives_zero_count(self):
        records = [_accepted_record(cost="0.001"), _accepted_record(cost="0.002")]
        s = summarize_usage(records)
        assert s.unknown_cost_count == 0


class TestFormatUsageSummary:
    def test_no_raw_content_only_counts_and_costs(self):
        records = [_accepted_record()]
        text = format_usage_summary(records, processed_count=1)
        assert "OpenRouter usage" in text
        assert "0.001" in text  # cost visible
        assert "vendor/a" in text  # model id visible (not confidential)

    def test_zero_processed_avoids_division_by_zero(self):
        text = format_usage_summary([], processed_count=0)
        assert "0" in text  # renders cleanly, no exception

    # --- F: summary labels the total incomplete when cost is unknown --------

    def test_f_unknown_cost_labels_total_incomplete_and_shows_count(self):
        records = [_accepted_record(cost=None), _accepted_record(cost="0.0123")]
        text = format_usage_summary(records, processed_count=1)
        assert "Requests with unknown cost: 1" in text
        assert "incomplete" in text
        assert "0.0123" in text  # the known partial total is still shown

    # --- G: with no unknown costs, the summary is unchanged from before -----

    def test_g_no_unknown_costs_summary_unchanged(self):
        records = [_accepted_record(cost="0.001"), _accepted_record(cost="0.002")]
        text = format_usage_summary(records, processed_count=1)
        assert "Requests with unknown cost" not in text
        assert "incomplete" not in text
        assert "Total cost (USD):     0.003" in text
