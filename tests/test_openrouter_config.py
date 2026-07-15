"""OpenRouter configuration parsing/validation (M1).

Covers: LLM_GATEWAY default/values, ordered text/vision model list parsing,
empty/malformed list rejection, and the guarantee that offline paths stay
key-free even when the openrouter gateway is selected.
"""

import pytest

from invoice_extractor.config import (
    ConfigurationError,
    load_config,
    validate_openrouter_config,
)

from .conftest import make_config

_OR_VARS = [
    "LLM_GATEWAY", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "OPENROUTER_TEXT_MODELS", "OPENROUTER_VISION_MODELS",
    "OPENROUTER_APP_NAME", "OPENROUTER_SITE_URL",
]


def _clean(monkeypatch):
    for var in _OR_VARS:
        monkeypatch.delenv(var, raising=False)


class TestGatewaySelection:
    def test_default_gateway_is_direct(self, monkeypatch):
        _clean(monkeypatch)
        assert load_config().llm_gateway == "direct"

    def test_openrouter_gateway_parsed_and_normalized(self, monkeypatch):
        _clean(monkeypatch)
        monkeypatch.setenv("LLM_GATEWAY", "  OpenRouter  ")
        assert load_config().llm_gateway == "openrouter"

    def test_invalid_gateway_raises_configuration_error(self, monkeypatch):
        _clean(monkeypatch)
        monkeypatch.setenv("LLM_GATEWAY", "bogus")
        with pytest.raises(ConfigurationError, match="LLM_GATEWAY"):
            load_config()

    def test_invalid_gateway_via_direct_construction_raises(self):
        with pytest.raises(ConfigurationError, match="LLM_GATEWAY"):
            make_config(llm_gateway="nope")


class TestModelListParsing:
    def test_defaults_are_empty_tuples(self, monkeypatch):
        _clean(monkeypatch)
        cfg = load_config()
        assert cfg.openrouter_text_models == ()
        assert cfg.openrouter_vision_models == ()

    def test_csv_parsed_stripped_empty_entries_preserved_for_rejection(self, monkeypatch):
        # Whitespace around entries is trimmed, but EMPTY entries are kept as
        # "" (not silently dropped) so validate_openrouter_config can reject
        # the stray-comma typo instead of quietly shrinking the ladder (M4).
        _clean(monkeypatch)
        monkeypatch.setenv("OPENROUTER_TEXT_MODELS", " vendor/a , vendor/b ,, vendor/c ")
        monkeypatch.setenv("OPENROUTER_VISION_MODELS", "vendor/x")
        cfg = load_config()
        assert cfg.openrouter_text_models == ("vendor/a", "vendor/b", "", "vendor/c")
        assert cfg.openrouter_vision_models == ("vendor/x",)

    def test_base_url_default_and_trailing_slash_stripped(self, monkeypatch):
        _clean(monkeypatch)
        assert load_config().openrouter_base_url == "https://openrouter.ai/api/v1"
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://example.test/api/v1/")
        assert load_config().openrouter_base_url == "https://example.test/api/v1"

    def test_app_name_default(self, monkeypatch):
        _clean(monkeypatch)
        assert load_config().openrouter_app_name == "Invoice Extractor"


class TestValidateOpenrouterConfig:
    def test_all_valid_does_not_raise(self):
        cfg = make_config(
            openrouter_api_key="test-or-key",
            openrouter_text_models=("vendor/text-a", "vendor/text-b"),
            openrouter_vision_models=("vendor/vision-a",),
        )
        validate_openrouter_config(cfg)  # no raise

    def test_missing_key_raises(self):
        cfg = make_config(
            openrouter_api_key=None,
            openrouter_text_models=("vendor/a",),
            openrouter_vision_models=("vendor/b",),
        )
        with pytest.raises(ConfigurationError, match="OPENROUTER_API_KEY"):
            validate_openrouter_config(cfg)

    def test_empty_text_list_raises(self):
        cfg = make_config(
            openrouter_api_key="k",
            openrouter_text_models=(),
            openrouter_vision_models=("vendor/b",),
        )
        with pytest.raises(ConfigurationError, match="OPENROUTER_TEXT_MODELS"):
            validate_openrouter_config(cfg)

    def test_empty_vision_list_raises(self):
        cfg = make_config(
            openrouter_api_key="k",
            openrouter_text_models=("vendor/a",),
            openrouter_vision_models=(),
        )
        with pytest.raises(ConfigurationError, match="OPENROUTER_VISION_MODELS"):
            validate_openrouter_config(cfg)

    @pytest.mark.parametrize("bad", ["noslash", "has space/model", "vendor/", "/model"])
    def test_malformed_model_id_raises(self, bad):
        cfg = make_config(
            openrouter_api_key="k",
            openrouter_text_models=(bad,),
            openrouter_vision_models=("vendor/ok",),
        )
        with pytest.raises(ConfigurationError, match="malformed model id"):
            validate_openrouter_config(cfg)

    def test_free_tag_id_is_accepted(self):
        cfg = make_config(
            openrouter_api_key="k",
            openrouter_text_models=("vendor/model:free",),
            openrouter_vision_models=("vendor/vision:beta",),
        )
        validate_openrouter_config(cfg)  # no raise

    def test_require_text_false_skips_text_list_for_image_only_files(self):
        # An image-only PDF's vision route must not demand a text model list
        # it will never use (M4).
        cfg = make_config(
            openrouter_api_key="k",
            openrouter_text_models=(),
            openrouter_vision_models=("vendor/vision-a",),
        )
        validate_openrouter_config(cfg, require_vision=True, require_text=False)  # no raise


class TestEmptyModelListEntries:
    """M4: a stray leading/double/trailing comma must be REJECTED with a
    clear error, never silently dropped - a silently shrunken ladder could
    mask a mistyped model id. Enforced for BOTH lists at live-call
    validation time (offline paths still never validate)."""

    @pytest.mark.parametrize("raw", [",vendor/a", "vendor/a,,vendor/b", "vendor/a,"])
    def test_vision_empty_entry_rejected(self, monkeypatch, raw):
        _clean(monkeypatch)
        monkeypatch.setenv("OPENROUTER_VISION_MODELS", raw)
        monkeypatch.setenv("OPENROUTER_TEXT_MODELS", "vendor/ok")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        cfg = load_config()
        with pytest.raises(ConfigurationError, match="OPENROUTER_VISION_MODELS.*empty entry"):
            validate_openrouter_config(cfg, require_vision=True)

    @pytest.mark.parametrize("raw", [",vendor/a", "vendor/a,,vendor/b", "vendor/a,"])
    def test_text_empty_entry_rejected(self, monkeypatch, raw):
        _clean(monkeypatch)
        monkeypatch.setenv("OPENROUTER_TEXT_MODELS", raw)
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        cfg = load_config()
        with pytest.raises(ConfigurationError, match="OPENROUTER_TEXT_MODELS.*empty entry"):
            validate_openrouter_config(cfg, require_vision=False)

    def test_whitespace_around_valid_entries_still_trimmed(self, monkeypatch):
        _clean(monkeypatch)
        monkeypatch.setenv("OPENROUTER_VISION_MODELS", " vendor/a , vendor/b ")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        cfg = load_config()
        assert cfg.openrouter_vision_models == ("vendor/a", "vendor/b")
        validate_openrouter_config(cfg, require_vision=True, require_text=False)  # no raise


class TestOfflineStaysKeyFree:
    """LLM_GATEWAY=openrouter with NO key and NO models must NOT break config
    loading - import/--help/classify/render/offline doctor call load_config and
    must remain key-free. Enforcement only happens at live-call time via
    validate_openrouter_config, which offline paths never call."""

    def test_load_config_openrouter_without_key_or_models_does_not_raise(self, monkeypatch):
        _clean(monkeypatch)
        monkeypatch.setenv("LLM_GATEWAY", "openrouter")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cfg = load_config()  # must not raise
        assert cfg.llm_gateway == "openrouter"
        assert cfg.openrouter_api_key is None
        assert cfg.openrouter_text_models == ()

    def test_describe_models_shows_gateway_without_keys(self):
        from invoice_extractor.config import describe_models
        cfg = make_config(
            llm_gateway="openrouter",
            openrouter_api_key="SECRET-OR-KEY",
            gemini_api_key="SECRET-GEM",
        )
        line = describe_models(cfg)
        assert "gateway=openrouter" in line
        assert "SECRET-OR-KEY" not in line
        assert "SECRET-GEM" not in line
