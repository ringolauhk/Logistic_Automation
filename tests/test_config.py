from decimal import Decimal

import pytest

from invoice_extractor.config import describe_models, load_config, provider_key_status
from invoice_extractor.pipeline import _chunked

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
        assert cfg.gemini_text_model == "gemini-flash-latest"
        assert cfg.gemini_vision_model == "gemini-flash-latest"
        assert cfg.claude_text_model == "claude-sonnet-5"
        assert cfg.claude_vision_model == "claude-sonnet-5"
        assert cfg.enable_claude_text_fallback is False  # original routing design
        assert cfg.save_debug_artifacts is False  # privacy default
        assert cfg.total_abs_tolerance == Decimal("0.02")
        assert cfg.total_rel_tolerance == Decimal("0.005")

    def test_default_gemini_model_is_not_a_known_retired_alias(self, monkeypatch):
        # Regression test for the 2026-07-13 live-doctor finding: "gemini-2.5-flash"
        # returns 404 "no longer available to new users" despite still being
        # enumerated by models.list(). Guards against reverting to it or another
        # known-retired alias without re-running `doctor --live` first.
        clean_env(monkeypatch)
        cfg = load_config()
        retired_aliases = {"gemini-2.5-flash", "gemini-pro-vision", "gemini-1.0-pro"}
        assert cfg.gemini_text_model not in retired_aliases
        assert cfg.gemini_vision_model not in retired_aliases


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


class TestMaxVisionPagesValidation:
    """Milestone 4 fix: MAX_VISION_PAGES must be >= 1, validated uniformly
    regardless of construction path (env var, .env, or direct Config(...)
    construction in tests) via Config.__post_init__. Confirms invalid
    configuration fails before any PDF or provider work starts - load_config()
    itself raises, nothing downstream ever runs."""

    def test_env_var_negative_one_raises(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("MAX_VISION_PAGES", "-1")
        with pytest.raises(ValueError, match="MAX_VISION_PAGES"):
            load_config()

    def test_env_var_zero_raises(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("MAX_VISION_PAGES", "0")
        with pytest.raises(ValueError, match="MAX_VISION_PAGES"):
            load_config()

    def test_env_var_one_is_valid(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.setenv("MAX_VISION_PAGES", "1")
        cfg = load_config()
        assert cfg.max_vision_pages == 1

    def test_env_var_non_integer_fails_via_int_conversion(self, monkeypatch):
        # int(os.getenv(...)) itself raises before Config() is even
        # constructed - already-clear existing behavior, locked down here.
        clean_env(monkeypatch)
        monkeypatch.setenv("MAX_VISION_PAGES", "abc")
        with pytest.raises(ValueError):
            load_config()

    def test_direct_config_construction_negative_raises(self):
        # Same rule applies when a Config is built directly (e.g. by a test
        # helper), not just via load_config().
        with pytest.raises(ValueError, match="MAX_VISION_PAGES"):
            make_config(max_vision_pages=-1)

    def test_direct_config_construction_zero_raises(self):
        with pytest.raises(ValueError, match="MAX_VISION_PAGES"):
            make_config(max_vision_pages=0)

    def test_error_message_names_field_and_requirement_no_secrets(self):
        try:
            make_config(gemini_api_key="SECRET-KEY-VALUE", max_vision_pages=-5)
        except ValueError as exc:
            message = str(exc)
            assert "MAX_VISION_PAGES" in message
            assert "-5" in message
            assert "SECRET-KEY-VALUE" not in message
        else:
            pytest.fail("expected ValueError")


class TestChunkedHelperValidation:
    """pipeline._chunked is small, pure, and directly importable - hardened
    here as defense in depth alongside the Config-level fix above."""

    def test_empty_items_size_one(self):
        assert _chunked([], 1) == []

    def test_three_items_size_one(self):
        assert _chunked([1, 2, 3], 1) == [[1], [2], [3]]

    def test_three_items_size_two(self):
        assert _chunked([1, 2, 3], 2) == [[1, 2], [3]]

    def test_size_zero_raises(self):
        with pytest.raises(ValueError):
            _chunked([1, 2, 3], 0)

    def test_size_negative_raises(self):
        with pytest.raises(ValueError):
            _chunked([1, 2, 3], -1)


class TestSecretsNeverInModelLog:
    def test_describe_models_contains_no_keys(self):
        cfg = make_config(gemini_api_key="SECRET-GEM", anthropic_api_key="SECRET-CLAUDE")
        line = describe_models(cfg)
        assert "SECRET-GEM" not in line
        assert "SECRET-CLAUDE" not in line
        assert "gemini-test-text" in line


class TestProviderKeyStatus:
    """The single place `doctor` and `run` both read "is this key
    configured" from - see config.py's provider_key_status docstring for why
    this replaced two separate bool(cfg.x_api_key) call sites."""

    def test_both_keys_set(self):
        cfg = make_config(gemini_api_key="x", anthropic_api_key="y")
        assert provider_key_status(cfg) == {"gemini": True, "anthropic": True}

    def test_both_keys_missing(self):
        cfg = make_config(gemini_api_key=None, anthropic_api_key=None)
        assert provider_key_status(cfg) == {"gemini": False, "anthropic": False}

    def test_only_gemini_set(self):
        cfg = make_config(gemini_api_key="x", anthropic_api_key=None)
        assert provider_key_status(cfg) == {"gemini": True, "anthropic": False}

    def test_empty_string_counts_as_missing(self):
        cfg = make_config(gemini_api_key="", anthropic_api_key=None)
        assert provider_key_status(cfg) == {"gemini": False, "anthropic": False}

    def test_never_leaks_key_values(self):
        cfg = make_config(gemini_api_key="SECRET-GEM", anthropic_api_key="SECRET-CLAUDE")
        status = provider_key_status(cfg)
        assert "SECRET-GEM" not in str(status)
        assert "SECRET-CLAUDE" not in str(status)
