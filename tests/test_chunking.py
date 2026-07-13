"""Vision-page chunking: MAX_VISION_PAGES is pages PER REQUEST; every
meaningful page is processed exactly once, in order, with per-chunk fallback.
All offline - provider seams are mocked; the network-block fixture guards.
"""

from pathlib import Path

from invoice_extractor import claude_client, gemini_client, pdf_utils
from invoice_extractor.pipeline import process_file

from .conftest import TEXT_BODY, invoice_json, make_config


def make_gemini_seam(cfg, text_responses=(), vision_responses=()):
    """Fake gemini_client._generate recording per-route calls.

    vision call record = number of image parts in the request.
    """
    calls = {"text": 0, "vision": []}
    text_queue, vision_queue = list(text_responses), list(vision_responses)

    def fake(cfg_, model, contents):
        if model == cfg.gemini_vision_model:
            calls["vision"].append(len(contents) - 1)  # first element is the prompt
            queue = vision_queue
        else:
            calls["text"] += 1
            queue = text_queue
        if not queue:
            raise AssertionError(f"unexpected extra call to {model}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fake, calls


def make_claude_seam(cfg, vision_responses=()):
    calls = {"vision": 0}
    queue = list(vision_responses)

    def fake(cfg_, model, content):
        assert model == cfg.claude_vision_model
        calls["vision"] += 1
        if not queue:
            raise AssertionError("unexpected extra Claude call")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fake, calls


def chunk_payload(n, description=None, **overrides):
    return invoice_json(line_items=[{
        "description": description or f"chunk{n} item",
        "quantity": 1, "unit_price": 10.0, "amount": 10.0,
    }], **overrides)


def scan_pdf(pdf_factory, n_pages, name="scan.pdf"):
    return Path(pdf_factory([("image",)] * n_pages, name=name))


class TestChunkSizing:
    def test_below_limit_single_request(self, logger, pdf_factory, monkeypatch):
        cfg = make_config(max_vision_pages=3)
        pdf = scan_pdf(pdf_factory, 2)
        fake, calls = make_gemini_seam(cfg, vision_responses=[invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", fake)
        result = process_file(pdf, cfg, logger)
        assert calls["vision"] == [2]  # one request carrying both pages
        assert result.vision_chunk_count == 1
        assert result.image_pages == [1, 2]
        assert result.failed_pages == []

    def test_exactly_at_limit_single_request(self, logger, pdf_factory, monkeypatch):
        cfg = make_config(max_vision_pages=3)
        pdf = scan_pdf(pdf_factory, 3)
        fake, calls = make_gemini_seam(cfg, vision_responses=[invoice_json()])
        monkeypatch.setattr(gemini_client, "_generate", fake)
        result = process_file(pdf, cfg, logger)
        assert calls["vision"] == [3]
        assert result.vision_chunk_count == 1

    def test_above_limit_every_page_exactly_once(self, logger, pdf_factory, monkeypatch):
        cfg = make_config(max_vision_pages=2)
        pdf = scan_pdf(pdf_factory, 5)
        payload = invoice_json()
        fake, calls = make_gemini_seam(cfg, vision_responses=[payload] * 3)
        monkeypatch.setattr(gemini_client, "_generate", fake)

        # Spy on rendering to capture the exact page numbers per chunk
        rendered_chunks = []
        real_render = pdf_utils.render_pages_png

        def spy(path, page_numbers, dpi=200):
            rendered_chunks.append(list(page_numbers))
            return real_render(path, page_numbers, dpi)

        monkeypatch.setattr(pdf_utils, "render_pages_png", spy)

        result = process_file(pdf, cfg, logger)
        assert calls["vision"] == [2, 2, 1]  # ordered chunks, every page covered
        assert rendered_chunks == [[1, 2], [3, 4], [5]]  # no omission, no overlap
        flat = [n for chunk in rendered_chunks for n in chunk]
        assert flat == sorted(set(flat)) == [1, 2, 3, 4, 5]
        assert result.vision_chunk_count == 3
        assert result.image_pages == [1, 2, 3, 4, 5]
        assert result.extraction_method == "vision"
        assert result.failed_pages == []


class TestChunkOrderingAndMerge:
    def test_line_items_ordered_by_original_page(self, logger, pdf_factory, monkeypatch):
        cfg = make_config(max_vision_pages=2)
        pdf = scan_pdf(pdf_factory, 5)
        fake, calls = make_gemini_seam(cfg, vision_responses=[
            chunk_payload(1), chunk_payload(2), chunk_payload(3),
        ])
        monkeypatch.setattr(gemini_client, "_generate", fake)
        result = process_file(pdf, cfg, logger)
        descriptions = [it.description for it in result.invoice.line_items]
        assert descriptions == ["chunk1 item", "chunk2 item", "chunk3 item"]

    def test_conflicting_fields_across_chunks_cause_review(
        self, logger, pdf_factory, monkeypatch
    ):
        cfg = make_config(max_vision_pages=1)
        pdf = scan_pdf(pdf_factory, 2)
        fake, _ = make_gemini_seam(cfg, vision_responses=[
            chunk_payload(1, total_amount=119.0),
            chunk_payload(2, total_amount=200.0),
        ])
        monkeypatch.setattr(gemini_client, "_generate", fake)
        result = process_file(pdf, cfg, logger)
        assert result.needs_review is True
        assert "conflict in total_amount" in result.review_reason
        # monetary conflicts keep the last-page chunk's value (flagged, not silent)
        assert float(result.invoice.total_amount) == 200.0


class TestChunkFailures:
    def test_middle_chunk_fails_both_providers_later_chunk_still_runs(
        self, logger, pdf_factory, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        pdf = scan_pdf(pdf_factory, 5)  # chunks: [1,2] [3,4] [5]
        gem, gem_calls = make_gemini_seam(cfg, vision_responses=[
            chunk_payload(1),
            "totally { broken json",  # chunk 2: Gemini unusable
            "still broken after repair retry",  # chunk 2: one repair attempt, also unusable
            chunk_payload(3),
        ])
        claude, claude_calls = make_claude_seam(cfg, vision_responses=[
            "also { broken",  # chunk 2: Claude unusable too
        ])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        monkeypatch.setattr(claude_client, "_request", claude)

        result = process_file(pdf, cfg, logger)
        # chunk 1 ok (2 imgs), chunk 2 fails + 1 repair retry (2 imgs each,
        # same images resent), chunk 3 ok (1 img) - all chunks still attempted
        assert gem_calls["vision"] == [2, 2, 2, 1]
        assert claude_calls["vision"] == 1  # fallback tried for the failed chunk only
        assert result.vision_chunk_count == 3
        assert result.failed_pages == [3, 4]
        assert result.needs_review is True
        assert result.error is False  # partial result, not a hard failure
        assert "pages 3-4" in result.review_reason  # failed range identified
        descriptions = [it.description for it in result.invoice.line_items]
        assert descriptions == ["chunk1 item", "chunk3 item"]  # survivors kept, in order

    def test_gemini_chunk_failure_recovered_by_claude(
        self, logger, pdf_factory, monkeypatch
    ):
        cfg = make_config(max_vision_pages=2)
        pdf = scan_pdf(pdf_factory, 3)  # chunks: [1,2] [3]
        gem, _ = make_gemini_seam(cfg, vision_responses=[
            chunk_payload(1),
            "broken {",  # chunk 2 gemini fails...
        ])
        claude, claude_calls = make_claude_seam(cfg, vision_responses=[
            chunk_payload(2, description="claude chunk2 item"),  # ...claude succeeds
        ])
        monkeypatch.setattr(gemini_client, "_generate", gem)
        monkeypatch.setattr(claude_client, "_request", claude)

        result = process_file(pdf, cfg, logger)
        assert claude_calls["vision"] == 1
        assert result.failed_pages == []
        assert result.provider == "mixed"  # chunk providers differ
        assert cfg.gemini_vision_model in result.model
        assert cfg.claude_vision_model in result.model
        descriptions = [it.description for it in result.invoice.line_items]
        assert descriptions == ["chunk1 item", "claude chunk2 item"]


class TestMixedDocumentWithChunks:
    def test_text_blank_and_multiple_image_chunks(self, logger, pdf_factory, monkeypatch):
        cfg = make_config(max_vision_pages=2)
        # page 1 text, pages 2-4 image, page 5 blank -> vision chunks [2,3] [4]
        pdf = Path(pdf_factory(
            [("text", TEXT_BODY), ("image",), ("image",), ("image",), ("blank",)],
            name="mixed_chunks.pdf",
        ))
        gem, calls = make_gemini_seam(
            cfg,
            text_responses=[chunk_payload(0, description="text item")],
            vision_responses=[chunk_payload(1), chunk_payload(2)],
        )
        monkeypatch.setattr(gemini_client, "_generate", gem)

        result = process_file(pdf, cfg, logger)
        assert result.document_classification == "mixed"
        assert result.extraction_method == "mixed"
        assert calls["text"] == 1
        assert calls["vision"] == [2, 1]
        assert result.text_pages == [1]
        assert result.image_pages == [2, 3, 4]
        assert result.blank_pages == [5]
        assert result.vision_chunk_count == 2
        descriptions = [it.description for it in result.invoice.line_items]
        assert descriptions == ["text item", "chunk1 item", "chunk2 item"]
