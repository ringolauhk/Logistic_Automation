"""Debug-artifact persistence: opt-in only, failures only, no secrets.

SAVE_DEBUG_ARTIFACTS is disabled by default; artifacts may contain full
invoice contents, so they must never be written unless explicitly enabled,
and the resulting files must not leak anything beyond what's passed in.
"""

from invoice_extractor import gemini_client
from invoice_extractor.artifacts import save_debug_artifact
from invoice_extractor.pipeline import process_file

from .conftest import TEXT_BODY, build_pdf, invoice_json, make_config


class TestSaveDebugArtifact:
    def test_disabled_by_default_writes_nothing(self, tmp_path):
        cfg = make_config(save_debug_artifacts=False, debug_artifact_dir=str(tmp_path))
        result = save_debug_artifact(
            cfg, "inv.pdf_gemini_text", model="gemini-test-text",
            reason="malformed JSON", raw_text="not json",
        )
        assert result is None
        assert list(tmp_path.iterdir()) == []

    def test_enabled_writes_one_file_with_metadata(self, tmp_path):
        cfg = make_config(save_debug_artifacts=True, debug_artifact_dir=str(tmp_path))
        path = save_debug_artifact(
            cfg, "inv.pdf_gemini_text", model="gemini-test-text",
            reason="malformed JSON in response", raw_text="{not valid",
        )
        assert path is not None
        assert path.exists()
        content = path.read_text()
        assert "label: inv.pdf_gemini_text" in content
        assert "model: gemini-test-text" in content
        assert "reason: malformed JSON in response" in content
        assert "{not valid" in content

    def test_filename_is_safe_for_unusual_labels(self, tmp_path):
        cfg = make_config(save_debug_artifacts=True, debug_artifact_dir=str(tmp_path))
        path = save_debug_artifact(
            cfg, "../../etc/passwd; rm -rf /", model="x", reason="x", raw_text="x",
        )
        assert path is not None
        # No path separator survives sanitization, so the whole label - even
        # one shaped like a traversal attempt - becomes a single, harmless
        # filename component; it can never resolve outside debug_artifact_dir.
        assert "/" not in path.name
        assert path.parent == tmp_path
        assert path.resolve().parent == tmp_path.resolve()

    def test_no_content_beyond_what_was_passed(self, tmp_path):
        cfg = make_config(
            save_debug_artifacts=True, debug_artifact_dir=str(tmp_path),
            gemini_api_key="SECRET-SHOULD-NOT-APPEAR",
        )
        path = save_debug_artifact(
            cfg, "inv.pdf_gemini_text", model="gemini-test-text",
            reason="malformed JSON", raw_text="plain response text",
        )
        assert "SECRET-SHOULD-NOT-APPEAR" not in path.read_text()

    def test_directory_created_if_missing(self, tmp_path):
        target = tmp_path / "nested" / "debug"
        cfg = make_config(save_debug_artifacts=True, debug_artifact_dir=str(target))
        path = save_debug_artifact(
            cfg, "inv.pdf_gemini_text", model="x", reason="x", raw_text="x",
        )
        assert path.exists()
        assert target.is_dir()


class TestPipelineArtifactIntegration:
    """Confirms the opt-in, failures-only behavior end to end through the
    real pipeline, not just the artifacts module in isolation."""

    def test_no_artifact_on_failure_when_disabled(self, logger, monkeypatch, tmp_path):
        cfg = make_config(save_debug_artifacts=False, debug_artifact_dir=str(tmp_path / "debug"))
        pdf_path = tmp_path / "text.pdf"
        build_pdf(pdf_path, [("text", TEXT_BODY)])
        monkeypatch.setattr(
            gemini_client, "_generate",
            lambda cfg_, model, contents: "not valid json at all",
        )
        result = process_file(pdf_path, cfg, logger)
        assert result.error is True  # sanity: this really did fail
        assert not (tmp_path / "debug").exists()

    def test_artifact_written_on_failure_when_enabled(self, logger, monkeypatch, tmp_path):
        debug_dir = tmp_path / "debug"
        cfg = make_config(save_debug_artifacts=True, debug_artifact_dir=str(debug_dir))
        pdf_path = tmp_path / "text.pdf"
        build_pdf(pdf_path, [("text", TEXT_BODY)])
        monkeypatch.setattr(
            gemini_client, "_generate",
            lambda cfg_, model, contents: "not valid json at all",
        )
        result = process_file(pdf_path, cfg, logger)
        assert result.error is True
        artifact_files = list(debug_dir.glob("*.txt"))
        assert len(artifact_files) == 1
        content = artifact_files[0].read_text()
        assert "gemini_text" in content
        assert "gemini-test-text" in content
        assert "not valid json at all" in content

    def test_no_artifact_on_success_even_when_enabled(self, logger, monkeypatch, tmp_path):
        debug_dir = tmp_path / "debug"
        cfg = make_config(save_debug_artifacts=True, debug_artifact_dir=str(debug_dir))
        pdf_path = tmp_path / "text.pdf"
        build_pdf(pdf_path, [("text", TEXT_BODY)])
        monkeypatch.setattr(
            gemini_client, "_generate",
            lambda cfg_, model, contents: invoice_json(),
        )
        result = process_file(pdf_path, cfg, logger)
        assert result.error is False
        assert not debug_dir.exists()  # a successful extraction writes nothing
