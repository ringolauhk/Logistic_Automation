import os
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    )


def describe_models(cfg: Config) -> str:
    """Startup log line: model names only - never API keys."""
    return (
        f"models: gemini_text={cfg.gemini_text_model} "
        f"gemini_vision={cfg.gemini_vision_model} "
        f"claude_text={cfg.claude_text_model} "
        f"claude_vision={cfg.claude_vision_model} "
        f"claude_text_fallback={'on' if cfg.enable_claude_text_fallback else 'off'}"
    )
