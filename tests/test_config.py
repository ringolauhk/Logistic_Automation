from decimal import Decimal

from invoice_extractor.config import describe_models, load_config

from .conftest import make_config

ALL_VARS = [
    "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_MODEL", "CLAUDE_MODEL",
    "GEMINI_TEXT_MODEL", "GEMINI_VISION_MODEL", "CLAUDE_TEXT_MODEL",
    "CLAUDE_VISION_MODEL", "ENABLE_CLAUDE_TEXT_FALLBACK", "RENDER_DPI",
    "TEXT_QUALITY_THRESHOLD", "MAX_VISION_PAGES", "MAX_RETRIES",
    "REQUEST_TIMEOUT_SECONDS", "TOTAL_ABS_TOLERANCE", "TOTAL_REL_TOLERANCE",
    "SAVE_DEBUG_ARTIFACTS", "DEBUG_ARTIFACT_DIR",
]


def clean_env(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


class TestDefaults:
    def test_default_models_and_flags(self, monkeypatch):
        clean_env(monkeypatch)
        cfg = load_config()
        assert cfg.gemini_text_model == "gemini-2.5-flash"
        assert cfg.gemini_vision_model == "gemini-2.5-flash"
        assert cfg.claude_text_model == "claude-sonnet-5"
        assert cfg.claude_vision_model == "claude-sonnet-5"
        assert cfg.enable_claude_text_fallback is False  # original routing design
        assert cfg.save_debug_artifacts is False  # privacy default
        assert cfg.total_abs_tolerance == Decimal("0.02")
        assert cfg.total_rel_tolerance == Decimal("0.005")


class TestOverrides:
    def test_per_route_model_vars(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-x-text")
        monkeypatch.setenv("GEMINI_VISION_MODEL", "gemini-x-vision")
        monkeypatch.setenv("CLAUDE_TEXT_MODEL", "claude-x-text")
        monkeypatch.setenv("CLAUDE_VISION_MODEL", "claude-x-vision")
        cfg = load_config()
        assert cfg.gemini_text_model == "gemini-x-text"
        assert cfg.gemini_vision_model == "gemini-x-vision"
        assert cfg.claude_text_model == "claude-x-text"
        assert cfg.claude_vision_model == "claude-x-vision"

    def test_legacy_single_model_vars_still_honored(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("GEMINI_MODEL", "gemini-legacy")
        monkeypatch.setenv("CLAUDE_MODEL", "claude-legacy")
        cfg = load_config()
        assert cfg.gemini_text_model == "gemini-legacy"
        assert cfg.gemini_vision_model == "gemini-legacy"
        assert cfg.claude_text_model == "claude-legacy"

    def test_fallback_flag_parsing(self, monkeypatch):
        clean_env(monkeypatch)
        for raw, expected in (("true", True), ("1", True), ("YES", True),
                              ("false", False), ("0", False), ("", False)):
            monkeypatch.setenv("ENABLE_CLAUDE_TEXT_FALLBACK", raw)
            assert load_config().enable_claude_text_fallback is expected, raw

    def test_tolerance_overrides(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("TOTAL_ABS_TOLERANCE", "0.10")
        monkeypatch.setenv("TOTAL_REL_TOLERANCE", "0.01")
        cfg = load_config()
        assert cfg.total_abs_tolerance == Decimal("0.10")
        assert cfg.total_rel_tolerance == Decimal("0.01")


class TestSecretsNeverInModelLog:
    def test_describe_models_contains_no_keys(self):
        cfg = make_config(gemini_api_key="SECRET-GEM", anthropic_api_key="SECRET-CLAUDE")
        line = describe_models(cfg)
        assert "SECRET-GEM" not in line
        assert "SECRET-CLAUDE" not in line
        assert "gemini-test-text" in line
