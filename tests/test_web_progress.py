"""M9 web UI: the events.jsonl/status.json protocol and the engine's
structured progress events (tests U-Z, AP, protocol rules). Offline."""

import dataclasses
import json
from pathlib import Path

from apps.web.progress import (
    SCHEMA_VERSION,
    EventWriter,
    build_status,
    read_events,
    read_status,
    write_status,
)
from invoice_extractor import gemini_client, openrouter_client
from invoice_extractor.events import ProgressEvent
from invoice_extractor.pipeline import process_directory

from .conftest import TEXT_BODY, build_pdf, invoice_json, make_config

# Every field a serialized event may carry (envelope + ProgressEvent fields).
APPROVED_FIELDS = {
    "schema_version", "seq", "ts", "job_id",
} | {f.name for f in dataclasses.fields(ProgressEvent)}


def _envelope(content, **usage):
    u = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
         "cost": 0.0001, "completion_tokens_details": {"reasoning_tokens": 0}}
    u.update(usage)
    return {"id": "gen-1", "model": "served-m",
            "choices": [{"finish_reason": "stop", "native_finish_reason": "STOP",
                         "message": {"content": content}}], "usage": u}


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, cfg, *, model, messages, response_format=None,
                 max_tokens, timeout=None):
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _or_cfg(**over):
    base = dict(llm_gateway="openrouter", openrouter_api_key="test-or-key",
                openrouter_text_models=("tv/text-1", "tv/text-2"),
                openrouter_vision_models=("tv/vis-1",), max_retries=1,
                max_text_pages=2, max_vision_pages=2)
    base.update(over)
    return make_config(**base)


def _collect(tmp_path, cfg, logger, responses, specs=(("text", TEXT_BODY),),
             monkeypatch=None):
    build_pdf(tmp_path / "inv.pdf", list(specs))
    monkeypatch.setattr(openrouter_client, "_chat_completion", Recorder(responses))
    events: list[ProgressEvent] = []
    process_directory(tmp_path, cfg, logger, on_event=events.append)
    return events


# --- events.jsonl protocol -------------------------------------------------------

class TestEventFileProtocol:
    def test_envelope_schema_seq_ts_jobid(self, tmp_path):
        writer = EventWriter(tmp_path / "events.jsonl", "job-x")
        writer(ProgressEvent(event="file_started", source_file="a.pdf",
                             file_index=1, file_total=2))
        writer(ProgressEvent(event="file_completed", source_file="a.pdf"))
        writer.close()
        events, malformed = read_events(tmp_path / "events.jsonl")
        assert malformed == 0
        assert [e["seq"] for e in events] == [1, 2]      # monotonic
        for e in events:
            assert e["schema_version"] == SCHEMA_VERSION
            assert e["job_id"] == "job-x"
            assert "ts" in e and "event" in e
            assert set(e) <= APPROVED_FIELDS

    def test_reader_tolerates_partial_final_line(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = EventWriter(path, "job-x")
        writer(ProgressEvent(event="file_started"))
        writer.close()
        with open(path, "a") as fh:
            fh.write('{"schema_version": 1, "seq": 2, "ev')  # torn write
        events, malformed = read_events(path)
        assert len(events) == 1
        assert malformed == 0                             # partial tail ignored

    def test_reader_skips_and_counts_malformed_middle_lines(self, tmp_path):
        path = tmp_path / "events.jsonl"
        lines = [json.dumps({"schema_version": 1, "seq": 1, "job_id": "j",
                             "ts": "t", "event": "file_started"}),
                 "THIS IS NOT JSON",
                 json.dumps({"schema_version": 1, "seq": 3, "job_id": "j",
                             "ts": "t", "event": "file_completed"})]
        path.write_text("\n".join(lines) + "\n")
        events, malformed = read_events(path)
        assert [e["seq"] for e in events] == [1, 3]
        assert malformed == 1

    def test_reader_ignores_duplicate_sequence_numbers(self, tmp_path):
        path = tmp_path / "events.jsonl"
        first = {"schema_version": 1, "seq": 1, "job_id": "j", "ts": "t",
                 "event": "file_started"}
        dupe = dict(first, event="file_failed")
        path.write_text(json.dumps(first) + "\n" + json.dumps(dupe) + "\n")
        events, _ = read_events(path)
        assert len(events) == 1
        assert events[0]["event"] == "file_started"        # first wins

    def test_broken_callback_never_breaks_extraction(self, tmp_path, logger,
                                                     monkeypatch):
        cfg = _or_cfg()

        def broken(_event):
            raise RuntimeError("UI exploded")
        build_pdf(tmp_path / "inv.pdf", [("text", TEXT_BODY)])
        monkeypatch.setattr(openrouter_client, "_chat_completion",
                            Recorder([_envelope(invoice_json())]))
        results = process_directory(tmp_path, cfg, logger, on_event=broken)
        assert results[0].error is False                   # run still succeeded


# --- status.json ------------------------------------------------------------------

class TestStatusFile:
    def test_atomic_write_and_fixed_schema(self, tmp_path):
        status = build_status("job-x", "running", created_at="t0")
        write_status(tmp_path, status)
        loaded = read_status(tmp_path)
        assert loaded["job_id"] == "job-x" and loaded["state"] == "running"
        assert set(loaded) == {"schema_version", "job_id", "state", "created_at",
                               "started_at", "finished_at", "exit_code", "summary",
                               "files", "artifacts", "error_category", "updated_at"}
        assert list(tmp_path.glob("*.tmp-*")) == []        # temp replaced


# --- U/V/W/X: engine emits provider events ----------------------------------------

class TestEngineProviderEvents:
    def test_u_event_before_text_provider_call(self, tmp_path, logger, monkeypatch):
        events = _collect(tmp_path, _or_cfg(), logger,
                          [_envelope(invoice_json())], monkeypatch=monkeypatch)
        kinds = [e.event for e in events]
        started = kinds.index("provider_request_started")
        assert kinds.index("chunk_started") < started
        ev = events[started]
        assert ev.route == "text" and ev.attempt_type == "primary"
        assert ev.requested_model == "tv/text-1"
        assert "provider_request_completed" in kinds

    def test_v_event_before_vision_provider_call(self, tmp_path, logger,
                                                 monkeypatch):
        events = _collect(tmp_path, _or_cfg(), logger,
                          [_envelope(invoice_json())], specs=[("image",)],
                          monkeypatch=monkeypatch)
        started = [e for e in events if e.event == "provider_request_started"]
        assert started and started[0].route == "vision"
        assert started[0].requested_model == "tv/vis-1"

    def test_w_repair_event(self, tmp_path, logger, monkeypatch):
        events = _collect(tmp_path, _or_cfg(), logger,
                          [_envelope("not valid json"),
                           _envelope(invoice_json())], monkeypatch=monkeypatch)
        repairs = [e for e in events if e.event == "provider_request_started"
                   and e.attempt_type == "repair"]
        assert len(repairs) == 1

    def test_x_escalation_event(self, tmp_path, logger, monkeypatch):
        err = openrouter_client.ProviderError("x", category="rate_limited",
                                              http_status=429)
        events = _collect(tmp_path, _or_cfg(), logger,
                          [err, _envelope(invoice_json())],
                          monkeypatch=monkeypatch)
        esc = [e for e in events if e.event == "provider_request_started"
               and e.attempt_type == "escalation"]
        assert len(esc) == 1 and esc[0].requested_model == "tv/text-2"

    def test_direct_gateway_emits_provider_events_too(self, tmp_path, logger,
                                                      monkeypatch):
        cfg = make_config()  # direct gateway
        build_pdf(tmp_path / "inv.pdf", [("text", TEXT_BODY)])
        monkeypatch.setattr(gemini_client, "_generate",
                            lambda c, m, ct: invoice_json())
        events: list[ProgressEvent] = []
        process_directory(tmp_path, cfg, logger, on_event=events.append)
        started = [e for e in events if e.event == "provider_request_started"]
        assert started and started[0].provider == "gemini"
        assert any(e.event == "provider_request_completed" and e.accepted
                   for e in events)


# --- Y/Z + AP: field allowlist & privacy ------------------------------------------

class TestEventPrivacy:
    def test_y_events_contain_only_approved_fields(self, tmp_path, logger,
                                                   monkeypatch):
        events = _collect(tmp_path, _or_cfg(), logger,
                          [_envelope(invoice_json())], monkeypatch=monkeypatch)
        for ev in events:
            payload = {k: v for k, v in dataclasses.asdict(ev).items()
                       if v is not None}
            assert set(payload) <= APPROVED_FIELDS

    def test_z_ap_events_exclude_sensitive_content(self, tmp_path, logger,
                                                   monkeypatch):
        secret_key = "SECRET-OR-KEY-M9"
        body = "UNIQUE-FAKE-INVOICE-BODY-M9"
        b64 = "RkFLRUJBU0U2NC1NOQ=="
        trace_marker = "Traceback (most recent call last)"
        cfg = _or_cfg(openrouter_api_key=secret_key)
        events = _collect(
            tmp_path, cfg, logger,
            [_envelope(f"not valid json {body} {b64}"),
             _envelope(f"still bad {body}"),
             _envelope(invoice_json())],
            monkeypatch=monkeypatch)
        # Serialize the full stream the way the worker would.
        writer_path = tmp_path / "events.jsonl"
        writer = EventWriter(writer_path, "job-x")
        for ev in events:
            writer(ev)
        writer.close()
        blob = writer_path.read_text()
        for forbidden in (secret_key, body, b64, trace_marker,
                          "Ocean freight", "INVOICE INV-1001",
                          "extraction engine", "data:image"):
            assert forbidden not in blob, f"leaked: {forbidden}"
