import pytest

from invoice_extractor.prompts import parse_json_response
from invoice_extractor.schema import ExtractionError

from .conftest import invoice_json


class TestParseJsonResponse:
    def test_plain_json(self):
        assert parse_json_response(invoice_json())["invoice_number"] == "INV-1001"

    def test_fenced_json(self):
        assert parse_json_response(f"```json\n{invoice_json()}\n```")["currency"] == "EUR"

    def test_preamble_and_trailer(self):
        text = f"Here is the extraction: {invoice_json()} Let me know if you need more."
        assert parse_json_response(text)["invoice_number"] == "INV-1001"

    def test_empty_response(self):
        with pytest.raises(ExtractionError):
            parse_json_response("")

    def test_malformed_json_raises_without_leaking_content(self):
        secret = "SELLER-CONFIDENTIAL-NAME-XYZ"
        with pytest.raises(ExtractionError) as excinfo:
            parse_json_response(f'{{"seller_name": "{secret}", broken')
        # loggable message must not embed response content...
        assert secret not in str(excinfo.value)
        # ...but the raw payload is available for opt-in debug artifacts
        assert secret in (excinfo.value.detail or "")

    def test_non_object_json_raises(self):
        with pytest.raises(ExtractionError):
            parse_json_response('["a", "list"]')

    def test_truncated_json_raises(self):
        # Cut off mid-array, as a response might be if generation stopped early.
        truncated = '{"invoice_number": "INV-1", "line_items": [{"description": "x"'
        with pytest.raises(ExtractionError):
            parse_json_response(truncated)

    def test_prose_with_no_json_at_all_raises(self):
        with pytest.raises(ExtractionError):
            parse_json_response("I'm sorry, I cannot process this document.")
