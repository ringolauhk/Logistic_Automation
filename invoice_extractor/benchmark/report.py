"""Benchmark report output: the 8-sheet report workbook + deterministic JSON
summary (M6). This is a SEPARATE artifact - it never touches the normal
extraction workbook's three-sheet contract.

Neither output ever contains prompts, provider responses, page text, base64,
images, or API keys - only expected/actual field values already present in
the validated ground truth and the extraction workbook.
"""

import json
from decimal import Decimal
from pathlib import Path

import openpyxl

from invoice_extractor.benchmark.scoring import BenchmarkReport

REPORT_SHEETS = [
    "Summary", "HeaderMetrics", "LineMetrics", "CaseResults",
    "LineMatches", "ReviewMetrics", "CostRuntime", "Errors",
]


def _write_sheet(wb, title, columns, rows):
    ws = wb.create_sheet(title)
    ws.append(columns)
    for row in rows:
        ws.append([row.get(c) for c in columns])


def _summary_rows(report: BenchmarkReport):
    a = report.aggregates
    pairs = [
        ("benchmark_cases", a["num_cases"]),
        ("matched_cases", a["matched_cases"]),
        ("outcome_extracted", a["outcome_extracted"]),
        ("outcome_needs_review", a["outcome_needs_review"]),
        ("outcome_failed", a["outcome_failed"]),
        ("header_micro_accuracy", a["header_micro_accuracy"]),
        ("header_macro_accuracy", a["header_macro_accuracy"]),
        ("exact_header_match_rate", a["exact_header_match_rate"]),
        ("required_field_completeness", a["required_field_completeness"]),
        ("line_precision", a["line_precision"]),
        ("line_recall", a["line_recall"]),
        ("line_f1", a["line_f1"]),
        ("matched_line_field_accuracy", a["matched_line_field_accuracy"]),
        ("numeric_field_accuracy", a["numeric_field_accuracy"]),
        ("invoice_all_lines_correct_rate", a["invoice_all_lines_correct_rate"]),
        ("needs_review_precision", a["review_precision"]),
        ("needs_review_recall", a["review_recall"]),
        ("needs_review_f1", a["review_f1"]),
        ("false_review_rate", a["false_review_rate"]),
        ("missed_problem_rate", a["missed_problem_rate"]),
        ("total_reported_cost", a["total_reported_cost"]),
        ("cost_incomplete", a["cost_incomplete"]),
        ("unknown_cost_requests", a["unknown_cost_requests"]),
        ("average_runtime_seconds", a["average_runtime_seconds"]),
        ("runtime_bases", ", ".join(a["runtime_bases"]) or "unknown"),
        ("fuzzy_line_matching_enabled", report.fuzzy_enabled),
        ("fuzzy_line_matches", a["fuzzy_line_matches"]),
        ("not_extractable_field_count", a["not_extractable_field_count"]),
        ("not_extractable_fields", ", ".join(a["not_extractable_fields"]) or "-"),
    ]
    rows = [{"metric": k, "value": _cell(v)} for k, v in pairs]
    for i, w in enumerate(a["weakest_fields"], start=1):
        rows.append({"metric": f"weakest_field_{i}",
                     "value": f"{w['scope']}.{w['field']} acc={w['accuracy']} (n={w['evaluated']})"})
    for t in report.threshold_results:
        rows.append({"metric": f"threshold:{t['threshold']}",
                     "value": f"target={t['target']} actual={t['actual']} "
                              f"{'PASS' if t['passed'] else 'FAIL'} {t['note']}".strip()})
    return rows


def _cell(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _header_metric_rows(report):
    a = report.aggregates
    # Per-field tallies rebuilt from cases (matched only).
    from invoice_extractor.benchmark.scoring import FieldTally
    agg: dict[str, FieldTally] = {}
    for c in report.cases:
        if c.invoice_status != "matched":
            continue
        for name, t in c.header_fields.items():
            agg.setdefault(name, FieldTally()).add(t)
    rows = []
    for name in sorted(agg):
        t = agg[name]
        rows.append({
            "field": name, "evaluated": t.evaluated, "correct": t.correct,
            "incorrect": t.incorrect, "missing": t.missing, "unexpected": t.unexpected,
            "accuracy": a["header_field_accuracy"].get(name),
            "precision": _safe_ratio(t.correct, t.correct + t.incorrect + t.unexpected),
            "recall": _safe_ratio(t.correct, t.correct + t.incorrect + t.missing),
        })
    return rows


def _safe_ratio(num, den):
    if den == 0:
        return None
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001")))


def _line_metric_rows(report):
    a = report.aggregates
    from invoice_extractor.benchmark.scoring import FieldTally
    agg: dict[str, FieldTally] = {}
    for c in report.cases:
        if c.invoice_status != "matched":
            continue
        for name, t in c.line_field_tallies.items():
            agg.setdefault(name, FieldTally()).add(t)
    rows = [{
        "metric": "line_detection",
        "expected": sum(c.line_counts.get("expected", 0) for c in report.cases),
        "actual": sum(c.line_counts.get("actual", 0) for c in report.cases),
        "matched": sum(c.line_counts.get("matched", 0) for c in report.cases),
        "missing": sum(c.line_counts.get("missing", 0) for c in report.cases),
        "extra": sum(c.line_counts.get("extra", 0) for c in report.cases),
        "ambiguous": sum(c.line_counts.get("ambiguous", 0) for c in report.cases),
        "fuzzy": a["fuzzy_line_matches"],
        "precision": a["line_precision"], "recall": a["line_recall"], "f1": a["line_f1"],
    }]
    for name in sorted(agg):
        t = agg[name]
        rows.append({"metric": f"field:{name}", "expected": None, "actual": None,
                     "matched": t.evaluated, "missing": t.missing, "extra": None,
                     "ambiguous": None, "fuzzy": None,
                     "precision": None, "recall": None,
                     "f1": a["line_field_accuracy"].get(name)})
    return [{k: _cell(v) for k, v in r.items()} for r in rows]


def _case_rows(report):
    rows = []
    for c in report.cases:
        lc = c.line_counts
        hf = c.header_fields
        rows.append({
            "case_id": c.case_id, "source_file": c.source_file,
            "document_type": c.document_type, "expected_outcome": c.expected_outcome,
            "actual_outcome": c.actual_outcome, "invoice_status": c.invoice_status,
            "header_correct": sum(t.correct for t in hf.values()),
            "header_incorrect": sum(t.incorrect for t in hf.values()),
            "header_missing": sum(t.missing for t in hf.values()),
            "header_unexpected": sum(t.unexpected for t in hf.values()),
            "exact_header_match": c.exact_header_match,
            "lines_expected": lc.get("expected"), "lines_actual": lc.get("actual"),
            "lines_matched": lc.get("matched"), "lines_missing": lc.get("missing"),
            "lines_extra": lc.get("extra"), "lines_ambiguous": lc.get("ambiguous"),
            "all_lines_correct": c.all_lines_correct,
            "expected_needs_review": c.expected_needs_review,
            "actual_needs_review": c.actual_needs_review,
            "review_class": c.review_class,
            "review_categories": ", ".join(c.actual_review_categories) or "-",
            "totals_flag_class": c.totals.get("totals_flag_class"),
            "reported_cost": c.cost.get("reported_cost"),
            "runtime_seconds": _cell(c.runtime_seconds),
            "runtime_basis": c.runtime_basis,
            "accepted_models": ", ".join(c.accepted_models) or "-",
            "routes": ", ".join(c.routes) or "-",
            "not_extractable_fields": ", ".join(c.not_extractable_fields) or "-",
            "ignored_fields": ", ".join(c.ignored_fields) or "-",
            "passed": c.passed, "notes": c.notes,
        })
    return rows


def _line_match_rows(report):
    rows = []
    for c in report.cases:
        for m in c.line_matches:
            rows.append({
                "case_id": c.case_id, "source_file": c.source_file,
                "expected_index": m["expected_index"], "actual_index": m["actual_index"],
                "method": m["method"], "confidence": m["confidence"],
                "row_correct": m["row_correct"],
            })
    return rows


def _review_rows(report):
    a = report.aggregates
    rows = [{
        "metric": "confusion", "TP": a["review_tp"], "TN": a["review_tn"],
        "FP": a["review_fp"], "FN": a["review_fn"],
        "precision": _cell(a["review_precision"]), "recall": _cell(a["review_recall"]),
        "f1": _cell(a["review_f1"]),
        "false_review_rate": _cell(a["false_review_rate"]),
        "missed_problem_rate": _cell(a["missed_problem_rate"]),
    }]
    for c in report.cases:
        if c.invoice_status != "matched":
            continue
        rows.append({
            "metric": f"case:{c.case_id}", "TP": None, "TN": None, "FP": None, "FN": None,
            "precision": c.review_class, "recall": ", ".join(c.actual_review_categories) or "-",
            "f1": ", ".join(c.unknown_review_clauses) or "-",
            "false_review_rate": None, "missed_problem_rate": None,
        })
    return rows


def _cost_runtime_rows(report):
    rows = []
    for c in report.cases:
        cost = c.cost
        rows.append({
            "scope": "case", "key": c.case_id, "document_type": c.document_type,
            "requests": cost.get("requests"), "primary": cost.get("primary"),
            "repair": cost.get("repair"), "escalation": cost.get("escalation"),
            "input_tokens": cost.get("input_tokens"), "output_tokens": cost.get("output_tokens"),
            "reasoning_tokens": cost.get("reasoning_tokens"), "total_tokens": cost.get("total_tokens"),
            "reported_cost": cost.get("reported_cost"),
            "unknown_cost_requests": cost.get("unknown_cost_requests"),
            "runtime_seconds": _cell(c.runtime_seconds), "runtime_basis": c.runtime_basis,
            "accepted_models": ", ".join(c.accepted_models) or "-",
            "routes": ", ".join(c.routes) or "-",
            "model_basis": None,
        })
    for d in report.doc_type_table:
        rows.append({
            "scope": "document_type", "key": d["document_type"], "document_type": d["document_type"],
            "requests": d["cases"], "primary": None, "repair": None, "escalation": None,
            "input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
            "total_tokens": None, "reported_cost": d["avg_cost"],
            "unknown_cost_requests": None,
            "runtime_seconds": d["avg_runtime"], "runtime_basis": "avg",
            "accepted_models": (f"med_cost={d['median_cost']} med_rt={d['median_runtime']} "
                                f"p95_rt={d['p95_runtime']} lpd={d['lines_per_dollar']} "
                                f"lpm={d['lines_per_minute']}"),
            "routes": (f"extracted={d['extracted']} review={d['needs_review']} "
                       f"failed={d['failed']}"),
            "model_basis": None,
        })
    for m in report.model_table:
        rows.append({
            "scope": "model", "key": m["model"], "document_type": None,
            "requests": m["requests"], "primary": m["primary"], "repair": m["repair"],
            "escalation": m["escalation"], "input_tokens": m["input_tokens"],
            "output_tokens": m["output_tokens"], "reasoning_tokens": None,
            "total_tokens": m["total_tokens"], "reported_cost": m["reported_cost"],
            "unknown_cost_requests": m["unknown_cost_requests"],
            "runtime_seconds": None, "runtime_basis": None,
            "accepted_models": None, "routes": None, "model_basis": m["model_basis"],
        })
    return rows


def write_report_workbook(report: BenchmarkReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default sheet; we create all 8 explicitly

    _write_sheet(wb, "Summary", ["metric", "value"], _summary_rows(report))
    _write_sheet(wb, "HeaderMetrics",
                 ["field", "evaluated", "correct", "incorrect", "missing", "unexpected",
                  "accuracy", "precision", "recall"], _header_metric_rows(report))
    _write_sheet(wb, "LineMetrics",
                 ["metric", "expected", "actual", "matched", "missing", "extra",
                  "ambiguous", "fuzzy", "precision", "recall", "f1"],
                 _line_metric_rows(report))
    _write_sheet(wb, "CaseResults",
                 ["case_id", "source_file", "document_type", "expected_outcome",
                  "actual_outcome", "invoice_status", "header_correct", "header_incorrect",
                  "header_missing", "header_unexpected", "exact_header_match",
                  "lines_expected", "lines_actual", "lines_matched", "lines_missing",
                  "lines_extra", "lines_ambiguous", "all_lines_correct",
                  "expected_needs_review", "actual_needs_review", "review_class",
                  "review_categories", "totals_flag_class", "reported_cost",
                  "runtime_seconds", "runtime_basis", "accepted_models", "routes",
                  "not_extractable_fields", "ignored_fields", "passed", "notes"],
                 _case_rows(report))
    _write_sheet(wb, "LineMatches",
                 ["case_id", "source_file", "expected_index", "actual_index",
                  "method", "confidence", "row_correct"], _line_match_rows(report))
    _write_sheet(wb, "ReviewMetrics",
                 ["metric", "TP", "TN", "FP", "FN", "precision", "recall", "f1",
                  "false_review_rate", "missed_problem_rate"], _review_rows(report))
    _write_sheet(wb, "CostRuntime",
                 ["scope", "key", "document_type", "requests", "primary", "repair",
                  "escalation", "input_tokens", "output_tokens", "reasoning_tokens",
                  "total_tokens", "reported_cost", "unknown_cost_requests",
                  "runtime_seconds", "runtime_basis", "accepted_models", "routes",
                  "model_basis"], _cost_runtime_rows(report))
    _write_sheet(wb, "Errors", ["case_id", "source_file", "category"], report.errors)
    wb.save(path)
    return path


def build_json_summary(report: BenchmarkReport) -> dict:
    """Deterministic machine-readable summary (sorted keys, Decimals as strings,
    cases in case_id order). No raw invoice text beyond expected/actual field
    values already present in the report."""
    cases = []
    for c in sorted(report.cases, key=lambda x: x.case_id):
        cases.append({
            "case_id": c.case_id, "source_file": c.source_file,
            "document_type": c.document_type, "expected_outcome": c.expected_outcome,
            "actual_outcome": c.actual_outcome, "invoice_status": c.invoice_status,
            "header": {n: {"correct": t.correct, "incorrect": t.incorrect,
                          "missing": t.missing, "unexpected": t.unexpected}
                      for n, t in sorted(c.header_fields.items())},
            "exact_header_match": c.exact_header_match,
            "required_complete": c.required_complete,
            "line_counts": c.line_counts,
            "all_lines_correct": c.all_lines_correct,
            "totals": c.totals,
            "expected_needs_review": c.expected_needs_review,
            "actual_needs_review": c.actual_needs_review,
            "review_class": c.review_class,
            "review_categories": list(c.actual_review_categories),
            "unknown_review_clauses": list(c.unknown_review_clauses),
            "cost": c.cost,
            "runtime_seconds": str(c.runtime_seconds) if c.runtime_seconds is not None else None,
            "runtime_basis": c.runtime_basis,
            "accepted_models": list(c.accepted_models),
            "routes": list(c.routes),
            "not_extractable_fields": list(c.not_extractable_fields),
            "ignored_fields": list(c.ignored_fields),
            "passed": c.passed,
        })
    return {
        "aggregates": report.aggregates,
        "cases": cases,
        "doc_type_table": report.doc_type_table,
        "model_table": report.model_table,
        "errors": report.errors,
        "thresholds": report.threshold_results,
        "fuzzy_enabled": report.fuzzy_enabled,
    }


def write_json_summary(report: BenchmarkReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_json_summary(report)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    return path
