import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
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
