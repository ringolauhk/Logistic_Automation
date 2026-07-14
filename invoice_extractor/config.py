import os
import re
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

# "gemini-2.5-flash" was confirmed via `doctor --live` (2026-07-13) to return
# a 404 "no longer available to new users" for current API keys, despite
# still being enumerated by models.list(). "gemini-flash-latest" is Google's
# alias for the current recommended flash model and was confirmed working
# for both text and vision via `doctor --live`.
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_APP_NAME = "Invoice Extractor"
GATEWAYS = ("direct", "openrouter")
# Structured-output request mode for the OpenRouter route. json_schema is
# preferred; runtime mode-fallback based on model capability is a later
# milestone (needs the model-metadata endpoint), so M2 uses one configured
# mode as-is. Values mirror provider.MODE_* string constants.
STRUCTURED_OUTPUT_MODES = ("json_schema", "json_object", "prompt_only")

# An OpenRouter model id is "vendor/model", optionally with a :tag suffix
# (e.g. "openai/gpt-5-mini", "mistralai/mistral-small-3.2-24b-instruct",
# "vendor/model:free"). Used only to catch typos - real capability is verified
# live against the models metadata endpoint in a later milestone.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+/[A-Za-z0-9._~:-]+$")


class ConfigurationError(Exception):
    """Invalid configuration (bad LLM_GATEWAY, malformed/empty OpenRouter
    model list, or missing OpenRouter key for a live run).

    Its message is authored to be safe to log - it never contains secrets or
    provider/response content - so logging_setup.exc_summary trusts it as a
    message-bearing type (like ExtractionError). Distinct from ExtractionError
    (which means "LLM output was unusable"): this means "the run is
    misconfigured", a different operator action.
    """


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv_models(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated model list, dropping blanks/whitespace."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _optional_int(raw: str | None) -> int | None:
    """Unset/empty means "no limit" (None), never a restrictive default."""
    if raw is None or not raw.strip():
        return None
    return int(raw)


def _optional_decimal(raw: str | None) -> Decimal | None:
    """Unset/empty means "no limit" (None), never a restrictive default."""
    if raw is None or not raw.strip():
        return None
    return Decimal(raw)


@dataclass(frozen=True)
class Config:
    gemini_api_key: str | None
    anthropic_api_key: str | None
    # Per-route model IDs. Defaults are current published aliases, but a model
    # is only confirmed working by `doctor --live` - never assume from the name.
    gemini_text_model: str
    gemini_vision_model: str
    claude_text_model: str
    claude_vision_model: str
    # Original spec only requires Claude fallback on the VISION path; the text
    # path stays Gemini-only unless this is explicitly enabled.
    enable_claude_text_fallback: bool
    render_dpi: int
    # A page counts as text-native when alnum chars on that page exceed this.
    text_quality_threshold: int
    # Cap on pages sent to the vision APIs per PDF (cost guard).
    max_vision_pages: int
    # Total attempts per provider call (1 initial + N-1 retries).
    max_retries: int
    # Per-request timeout applied to both providers.
    request_timeout_seconds: int
    # Totals reconciliation tolerances.
    total_abs_tolerance: Decimal
    total_rel_tolerance: Decimal
    # When true, raw provider responses are persisted for debugging.
    # WARNING: artifacts may contain confidential invoice data.
    save_debug_artifacts: bool
    debug_artifact_dir: str

    # --- OpenRouter gateway (M1: config only; pipeline is not switched yet) --
    # These all carry safe defaults so the direct path and existing tests are
    # unaffected. LLM_GATEWAY selects direct (default, rollback) vs openrouter.
    llm_gateway: str = "direct"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openrouter_text_models: tuple[str, ...] = ()
    openrouter_vision_models: tuple[str, ...] = ()
    openrouter_app_name: str = DEFAULT_OPENROUTER_APP_NAME
    openrouter_site_url: str | None = None
    openrouter_structured_output: str = "json_schema"
    # Bounded-spend controls (M3). None/unset = no limit - never a silently
    # restrictive default. See pipeline.py/openrouter_client.py for exactly
    # how each is enforced (per-file model-ladder cap vs. run-wide cost cap).
    max_model_attempts_per_file: int | None = None
    max_cost_usd_per_file: Decimal | None = None
    max_cost_usd_per_run: Decimal | None = None
    # Cap on text pages sent PER OpenRouter request (M3.1 bounded chunking) -
    # only the OpenRouter text ladder chunks; the direct Gemini/Claude text
    # route is unaffected (see pipeline.process_file). Default 2: the pilot's
    # 6-page invoice truncated (finish_reason=length) on all 3 configured
    # models at a whole-document single request; 2 pages keeps each request's
    # JSON output comfortably under typical output-token caps while keeping
    # the per-file request count (and therefore cost) low. Must be >= 1.
    max_text_pages: int = 2

    def __post_init__(self) -> None:
        # Runs on EVERY construction path - load_config() and direct
        # `Config(...)` calls alike (frozen dataclasses still run
        # __post_init__; it just can't assign attributes, which we don't
        # need to here). This is the fix for the confirmed bug where a
        # negative MAX_VISION_PAGES let pipeline._chunked silently return no
        # chunks at all (range(0, n, -1) is empty), causing an image-only
        # PDF to be misreported as having no meaningful pages instead of
        # failing loudly at startup.
        if self.max_vision_pages < 1:
            raise ValueError(
                f"MAX_VISION_PAGES must be at least 1 (got {self.max_vision_pages}); "
                "check your .env or environment configuration."
            )
        if self.max_text_pages < 1:
            raise ValueError(
                f"MAX_TEXT_PAGES must be at least 1 (got {self.max_text_pages}); "
                "check your .env or environment configuration."
            )
        # Gateway VALUE is validated here (a bare typo, key-independent, worth
        # failing fast on any command). Key presence and model-list contents
        # are deliberately NOT validated here - see validate_openrouter_config
        # - so import/--help/classify/render/offline doctor stay key-free even
        # when LLM_GATEWAY=openrouter is configured with no key or models yet.
        if self.llm_gateway not in GATEWAYS:
            raise ConfigurationError(
                f"LLM_GATEWAY must be one of {GATEWAYS} (got {self.llm_gateway!r})"
            )
        if self.openrouter_structured_output not in STRUCTURED_OUTPUT_MODES:
            raise ConfigurationError(
                f"OPENROUTER_STRUCTURED_OUTPUT must be one of {STRUCTURED_OUTPUT_MODES} "
                f"(got {self.openrouter_structured_output!r})"
            )
        if self.max_model_attempts_per_file is not None and self.max_model_attempts_per_file < 1:
            raise ConfigurationError(
                "MAX_MODEL_ATTEMPTS_PER_FILE must be at least 1 if set "
                f"(got {self.max_model_attempts_per_file})"
            )
        if self.max_cost_usd_per_file is not None and self.max_cost_usd_per_file < 0:
            raise ConfigurationError(
                f"MAX_COST_USD_PER_FILE must be non-negative if set "
                f"(got {self.max_cost_usd_per_file})"
            )
        if self.max_cost_usd_per_run is not None and self.max_cost_usd_per_run < 0:
            raise ConfigurationError(
                f"MAX_COST_USD_PER_RUN must be non-negative if set "
                f"(got {self.max_cost_usd_per_run})"
            )


def load_config() -> Config:
    # Legacy single-model vars are honored as defaults for the per-route vars.
    gemini_default = os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    claude_default = os.getenv("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL
    return Config(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL") or gemini_default,
        gemini_vision_model=os.getenv("GEMINI_VISION_MODEL") or gemini_default,
        claude_text_model=os.getenv("CLAUDE_TEXT_MODEL") or claude_default,
        claude_vision_model=os.getenv("CLAUDE_VISION_MODEL") or claude_default,
        enable_claude_text_fallback=_env_bool("ENABLE_CLAUDE_TEXT_FALLBACK", False),
        render_dpi=int(os.getenv("RENDER_DPI", "200")),
        text_quality_threshold=int(os.getenv("TEXT_QUALITY_THRESHOLD", "20")),
        max_vision_pages=int(os.getenv("MAX_VISION_PAGES", "5")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
        total_abs_tolerance=Decimal(os.getenv("TOTAL_ABS_TOLERANCE", "0.02")),
        total_rel_tolerance=Decimal(os.getenv("TOTAL_REL_TOLERANCE", "0.005")),
        save_debug_artifacts=_env_bool("SAVE_DEBUG_ARTIFACTS", False),
        debug_artifact_dir=os.getenv("DEBUG_ARTIFACT_DIR", "./output/debug"),
        llm_gateway=(os.getenv("LLM_GATEWAY") or "direct").strip().lower(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_base_url=(
            os.getenv("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
        ).rstrip("/"),
        openrouter_text_models=_csv_models(os.getenv("OPENROUTER_TEXT_MODELS")),
        openrouter_vision_models=_csv_models(os.getenv("OPENROUTER_VISION_MODELS")),
        openrouter_app_name=os.getenv("OPENROUTER_APP_NAME") or DEFAULT_OPENROUTER_APP_NAME,
        openrouter_site_url=os.getenv("OPENROUTER_SITE_URL") or None,
        openrouter_structured_output=(
            os.getenv("OPENROUTER_STRUCTURED_OUTPUT") or "json_schema"
        ).strip().lower(),
        max_model_attempts_per_file=_optional_int(os.getenv("MAX_MODEL_ATTEMPTS_PER_FILE")),
        max_cost_usd_per_file=_optional_decimal(os.getenv("MAX_COST_USD_PER_FILE")),
        max_cost_usd_per_run=_optional_decimal(os.getenv("MAX_COST_USD_PER_RUN")),
        max_text_pages=int(os.getenv("MAX_TEXT_PAGES", "2")),
    )


def _validate_model_ids(models: tuple[str, ...], var_name: str) -> None:
    if not models:
        raise ConfigurationError(
            f"{var_name} is empty; configure at least one OpenRouter model id "
            "(comma-separated) to use the openrouter gateway."
        )
    for model in models:
        if not _MODEL_ID_RE.match(model):
            raise ConfigurationError(
                f"{var_name} contains a malformed model id: {model!r} "
                "(expected 'vendor/model')."
            )


def validate_openrouter_config(cfg: "Config", *, require_vision: bool = True) -> None:
    """Raise ConfigurationError if the OpenRouter gateway is not usable for a
    LIVE run (missing key, empty list, or malformed model id).

    Called only immediately before making real OpenRouter calls - NEVER on
    offline paths (import/--help/classify/render/offline doctor), so those
    remain key- and model-list-free even when LLM_GATEWAY=openrouter is set.

    require_vision=False is used by the text-only path (M2): a vision model
    list is not required when only text extraction runs through OpenRouter.
    """
    if not cfg.openrouter_api_key:
        raise ConfigurationError("OPENROUTER_API_KEY is not set")
    _validate_model_ids(cfg.openrouter_text_models, "OPENROUTER_TEXT_MODELS")
    if require_vision:
        _validate_model_ids(cfg.openrouter_vision_models, "OPENROUTER_VISION_MODELS")


def describe_models(cfg: Config) -> str:
    """Startup log line: gateway + model names only - never API keys."""
    return (
        f"gateway={cfg.llm_gateway} "
        f"models: gemini_text={cfg.gemini_text_model} "
        f"gemini_vision={cfg.gemini_vision_model} "
        f"claude_text={cfg.claude_text_model} "
        f"claude_vision={cfg.claude_vision_model} "
        f"claude_text_fallback={'on' if cfg.enable_claude_text_fallback else 'off'}"
    )


def provider_key_status(cfg: Config) -> dict[str, bool]:
    """Single source of truth for "is this provider's key configured" -
    booleans only, never the key values themselves. `doctor` and `run` both
    report this same status (for different purposes: an upfront console
    warning vs. a health-check display) and previously each recomputed
    `bool(cfg.x_api_key)` separately; this is the one place that fact is
    derived. Actual enforcement (raising when a call is attempted without a
    key) still lives where it belongs - in each provider client's own
    _get_client() - this function never raises, only reports.
    """
    return {
        "gemini": bool(cfg.gemini_api_key),
        "anthropic": bool(cfg.anthropic_api_key),
    }
