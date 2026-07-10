"""Shared fixtures: network blocking, no-sleep retries, config + PDF factories.

The suite requires no API keys, makes no network calls, generates its own
synthetic PDF fixtures under pytest tmp_path (auto-cleaned), and never sleeps
for real retry delays.
"""

import json
import logging
import socket
from decimal import Decimal

import fitz
import pytest
from tenacity import wait_none

from invoice_extractor import retry
from invoice_extractor.config import Config


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Any accidentally unmocked API call fails immediately."""

    def guard(*args, **kwargs):
        raise RuntimeError("network access is blocked during tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Retries never sleep for real delays in tests."""
    monkeypatch.setattr(retry, "WAIT_STRATEGY", wait_none())


def make_config(**overrides) -> Config:
    base = dict(
        gemini_api_key="test-gemini-key",
        anthropic_api_key="test-anthropic-key",
        gemini_text_model="gemini-test-text",
        gemini_vision_model="gemini-test-vision",
        claude_text_model="claude-test-text",
        claude_vision_model="claude-test-vision",
        enable_claude_text_fallback=False,
        render_dpi=100,
        text_quality_threshold=20,
        max_vision_pages=5,
        max_retries=3,
        request_timeout_seconds=5,
        total_abs_tolerance=Decimal("0.02"),
        total_rel_tolerance=Decimal("0.005"),
        save_debug_artifacts=False,
        debug_artifact_dir="./output/debug",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def logger() -> logging.Logger:
    lg = logging.getLogger("invoice_extractor_tests")
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


# --- synthetic invoice JSON (what a well-behaved provider would return) -----

def invoice_dict(**overrides) -> dict:
    data = {
        "invoice_number": "INV-1001",
        "invoice_date": "2026-07-01",
        "currency": "EUR",
        "seller_name": "Acme Logistics GmbH",
        "seller_address": "Hafenstrasse 12, Hamburg",
        "buyer_name": "Global Trade Ltd",
        "buyer_address": "1 Harbour Rd, Hong Kong",
        "subtotal": 100.0,
        "tax_amount": 19.0,
        "total_amount": 119.0,
        "payment_terms": "Net 30",
        "line_items": [
            {"description": "Ocean freight", "quantity": 1,
             "unit_price": 100.0, "amount": 100.0},
        ],
    }
    data.update(overrides)
    return data


def invoice_json(**overrides) -> str:
    return json.dumps(invoice_dict(**overrides))


# --- synthetic PDF fixtures ---------------------------------------------------

TEXT_BODY = (
    "INVOICE INV-1001 dated 2026-07-01 issued by Acme Logistics GmbH Hamburg "
    "to Global Trade Ltd Hong Kong. Ocean freight 100.00 EUR, VAT 19.00, "
    "total 119.00. Payment terms Net 30."
)


def _scan_png() -> bytes:
    """A rendered 'scan' image to embed in image-only pages."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((20, 60), "SCANNED INVOICE PAGE", fontsize=14)
    png = page.get_pixmap().tobytes("png")
    doc.close()
    return png


def build_pdf(path, page_specs) -> str:
    """page_specs: list of ('text', body) | ('image',) | ('blank',) tuples."""
    doc = fitz.open()
    for spec in page_specs:
        page = doc.new_page()
        if spec[0] == "text":
            page.insert_text((50, 72), spec[1], fontsize=10)
        elif spec[0] == "image":
            page.insert_image(fitz.Rect(40, 40, 460, 460), stream=_scan_png())
        elif spec[0] != "blank":
            raise ValueError(f"unknown page spec {spec!r}")
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def pdf_factory(tmp_path):
    counter = {"n": 0}

    def factory(page_specs, name: str | None = None) -> str:
        counter["n"] += 1
        return build_pdf(tmp_path / (name or f"fixture_{counter['n']}.pdf"), page_specs)

    return factory
