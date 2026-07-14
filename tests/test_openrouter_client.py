"""OpenRouter boundary (M1): response normalization, error normalization, and
privacy. All offline - the network-blocking autouse fixture guards; the only
mocked seam is the httpx client (or _chat_completion / _get_client)."""

import httpx
import pytest

from invoice_extractor import openrouter_client as orc
from invoice_extractor.logging_setup import exc_summary
from invoice_extractor.provider import ProviderError, is_truncated

from .conftest import make_config


def _success_envelope(**over):
    raw = {
        "id": "gen-xyz",
        "model": "vendor/actual-model",
        "choices": [{
            "finish_reason": "stop",
            "native_finish_reason": "STOP",
            "message": {"content": '{"invoice_number": "INV-1"}'},
        }],
        "usage": {
            "prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250,
            "cost": 0.00035,
            "completion_tokens_details": {"reasoning_tokens": 30},
        },
    }
    raw.update(over)
    return raw


class _FakeResponse:
    def __init__(self, status_code, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = []

    def post(self, path, json=None, timeout=None):
        self.calls.append({"path": path, "json": json})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _install_fake_client(monkeypatch, fake):
    monkeypatch.setattr(orc, "_get_client", lambda cfg: fake)


class TestParseCompletion:
    def test_normalizes_all_fields(self):
        r = orc.parse_completion(_success_envelope(), requested_model="vendor/requested",
                                 route="text")
        assert r.requested_model == "vendor/requested"
        assert r.actual_model == "vendor/actual-model"  # actual differs from requested
        assert r.generation_id == "gen-xyz"
        assert r.input_tokens == 1000 and r.output_tokens == 250 and r.total_tokens == 1250
        assert r.reasoning_tokens == 30
        assert str(r.cost_usd) == "0.00035"
        assert r.finish_reason == "stop" and r.native_finish_reason == "STOP"
        assert is_truncated(r) is False

    def test_actual_model_differs_from_requested_recorded(self):
        r = orc.parse_completion(_success_envelope(model="vendor/served-elsewhere"),
                                 requested_model="vendor/asked", route="text")
        assert r.requested_model == "vendor/asked"
        assert r.actual_model == "vendor/served-elsewhere"

    def test_finish_reason_length_flags_truncation(self):
        raw = _success_envelope()
        raw["choices"][0]["finish_reason"] = "length"
        raw["choices"][0]["message"]["content"] = '{"invoice_number": "INV-1", "line_'
        assert is_truncated(orc.parse_completion(raw, requested_model="m", route="text"))

    def test_missing_usage_yields_none_tokens(self):
        raw = _success_envelope()
        del raw["usage"]
        r = orc.parse_completion(raw, requested_model="m", route="text")
        assert r.input_tokens is None and r.cost_usd is None and r.reasoning_tokens is None

    def test_empty_content_is_normalized_not_error(self):
        raw = _success_envelope()
        raw["choices"][0]["message"]["content"] = None
        r = orc.parse_completion(raw, requested_model="m", route="text")
        assert r.text == ""

    @pytest.mark.parametrize("bad", [
        {"choices": []},
        {"choices": "nope"},
        {"no_choices": True},
        {"choices": [{"message": {"content": 123}}]},
        "not-a-dict",
    ])
    def test_malformed_envelope_raises_sanitized_provider_error(self, bad):
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(bad, requested_model="m", route="text")
        assert ei.value.category == "malformed_envelope"


class TestEmbeddedErrorEnvelope:
    """A 200-status HTTP response whose JSON body carries {"error": {...}}
    instead of "choices" - the exact shape a live pilot hit for an
    unsupported response_format. Only the numeric code is ever inspected;
    message/metadata are untrusted and never surfaced."""

    def test_embedded_400_maps_to_client_error(self):
        raw = {"id": "gen-1", "error": {"code": 400, "message": "FAKE-SECRET-BODY"}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "client_error"
        assert ei.value.http_status == 400
        assert "FAKE-SECRET-BODY" not in str(ei.value)

    def test_embedded_402_maps_to_payment_required(self):
        raw = {"id": "gen-1", "error": {"code": 402, "message": "FAKE-INVOICE-TEXT"}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "payment_required"
        assert ei.value.http_status == 402
        assert "FAKE-INVOICE-TEXT" not in str(ei.value)

    def test_embedded_429_maps_to_rate_limited(self):
        raw = {"id": "gen-1", "error": {"code": 429, "message": "FAKE-SECRET"}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "rate_limited"
        assert ei.value.http_status == 429
        assert "FAKE-SECRET" not in str(ei.value)

    def test_embedded_5xx_maps_to_server_error(self):
        raw = {"id": "gen-1", "error": {"code": 503, "message": "FAKE-METADATA-BODY",
                                        "metadata": {"raw": "FAKE-RAW-BODY"}}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "server_error"
        assert ei.value.http_status == 503
        assert "FAKE-METADATA-BODY" not in str(ei.value)
        assert "FAKE-RAW-BODY" not in str(ei.value)

    def test_embedded_404_maps_to_model_unavailable(self):
        raw = {"id": "gen-1", "error": {"code": 404, "message": "unknown model"}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "model_unavailable"
        assert ei.value.http_status == 404

    def test_no_choices_and_no_error_remains_generic(self):
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion({"id": "gen-1"}, requested_model="m", route="text")
        assert ei.value.category == "malformed_envelope"
        assert ei.value.http_status is None

    def test_error_object_without_numeric_code_is_malformed_envelope(self):
        raw = {"id": "gen-1", "error": {"message": "FAKE-SECRET-NO-CODE"}}
        with pytest.raises(ProviderError) as ei:
            orc.parse_completion(raw, requested_model="m", route="text")
        assert ei.value.category == "malformed_envelope"
        assert "FAKE-SECRET-NO-CODE" not in str(ei.value)


class TestChatCompletionErrorNormalization:
    def test_success_returns_raw_dict(self, monkeypatch):
        fake = _FakeClient(response=_FakeResponse(200, _success_envelope()))
        _install_fake_client(monkeypatch, fake)
        raw = orc._chat_completion(make_config(openrouter_api_key="k"),
                                   model="vendor/m", messages=[{"role": "user", "content": "x"}],
                                   max_tokens=8192)
        assert raw["id"] == "gen-xyz"
        assert fake.calls[0]["path"] == "/chat/completions"

    def test_http_429_normalized(self, monkeypatch):
        body = {"error": {"message": "quota INVOICE-SECRET; ref=999", "code": "rate_limited"}}
        fake = _FakeClient(response=_FakeResponse(429, body))
        _install_fake_client(monkeypatch, fake)
        with pytest.raises(ProviderError) as ei:
            orc._chat_completion(make_config(openrouter_api_key="k"),
                                 model="vendor/m", messages=[], max_tokens=1)
        exc = ei.value
        assert exc.category == "rate_limited" and exc.http_status == 429
        assert "HTTP 429" in str(exc)
        assert "INVOICE-SECRET" not in str(exc)  # body never surfaced

    def test_http_500_normalized(self, monkeypatch):
        fake = _FakeClient(response=_FakeResponse(503, {"error": {"message": "SECRET"}}))
        _install_fake_client(monkeypatch, fake)
        with pytest.raises(ProviderError) as ei:
            orc._chat_completion(make_config(openrouter_api_key="k"),
                                 model="vendor/m", messages=[], max_tokens=1)
        assert ei.value.category == "server_error" and ei.value.http_status == 503
        assert "SECRET" not in str(ei.value)

    def test_transport_error_normalized(self, monkeypatch):
        fake = _FakeClient(raise_exc=httpx.ConnectError("cannot connect to host SECRET-HOST"))
        _install_fake_client(monkeypatch, fake)
        with pytest.raises(ProviderError) as ei:
            orc._chat_completion(make_config(openrouter_api_key="k"),
                                 model="vendor/m", messages=[], max_tokens=1)
        assert ei.value.category == "transport"
        assert "SECRET-HOST" not in str(ei.value)

    def test_non_json_200_is_malformed_envelope(self, monkeypatch):
        fake = _FakeClient(response=_FakeResponse(200, raise_json=True))
        _install_fake_client(monkeypatch, fake)
        with pytest.raises(ProviderError) as ei:
            orc._chat_completion(make_config(openrouter_api_key="k"),
                                 model="vendor/m", messages=[], max_tokens=1)
        assert ei.value.category == "malformed_envelope"

    def test_missing_key_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
            orc._get_client(make_config(openrouter_api_key=None))


class TestNetworkIsBlockedExceptAtBoundary:
    def test_real_chat_completion_hits_the_network_block(self):
        # No mock: a real httpx call must be stopped by the autouse
        # block_network fixture, never reach the internet. It surfaces as a
        # normalized transport ProviderError (or the block RuntimeError) -
        # either way, no real egress.
        cfg = make_config(openrouter_api_key="k",
                          openrouter_base_url="https://openrouter.ai/api/v1")
        orc._client = None  # force a fresh client
        with pytest.raises(Exception) as ei:
            orc._chat_completion(cfg, model="vendor/m",
                                 messages=[{"role": "user", "content": "x"}], max_tokens=1)
        orc._client = None
        assert not isinstance(ei.value, AssertionError)


class TestPrivacyRegression:
    def test_no_secret_key_invoice_text_or_body_in_error_summary(self, monkeypatch):
        # Distinct fakes in: API key, error body, and (would-be) invoice text.
        body = {"error": {"message": "FAKE-INVOICE-TEXT-77 leaked in body",
                          "metadata": {"raw": "FAKE-PROVIDER-BODY-88"}}}
        fake = _FakeClient(response=_FakeResponse(429, body))
        _install_fake_client(monkeypatch, fake)
        cfg = make_config(openrouter_api_key="SECRET-OR-KEY-99")
        with pytest.raises(ProviderError) as ei:
            orc._chat_completion(cfg, model="vendor/m", messages=[], max_tokens=1)
        summary = exc_summary(ei.value)
        for secret in ("FAKE-INVOICE-TEXT-77", "FAKE-PROVIDER-BODY-88", "SECRET-OR-KEY-99"):
            assert secret not in str(ei.value)
            assert secret not in summary


class TestPayloadBuilders:
    def test_text_messages_shape(self):
        assert orc.build_text_messages("hello") == [{"role": "user", "content": "hello"}]

    def test_vision_messages_shape_multiple_images(self):
        msgs = orc.build_vision_messages("extract", [b"png1", b"png2"])
        content = msgs[0]["content"]
        images = [c for c in content if c["type"] == "image_url"]
        assert len(images) == 2  # multiple images per request
        assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert content[-1] == {"type": "text", "text": "extract"}

    def test_response_format_modes(self):
        assert orc.build_response_format("prompt_only") is None
        assert orc.build_response_format("json_object") == {"type": "json_object"}
        js = orc.build_response_format("json_schema")
        assert js["type"] == "json_schema"
        assert js["json_schema"]["strict"] is True
        assert js["json_schema"]["name"] == "invoice"
        assert isinstance(js["json_schema"]["schema"], dict)
