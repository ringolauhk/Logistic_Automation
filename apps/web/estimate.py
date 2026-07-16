"""Conservative UPPER-BOUND provider-attempt estimate for the Start screen
(M9, adjustment 5).

This is NOT exact and is never presented as exact: real runs stop early on
the first accepted model, on validation acceptance, and on file/run budget
crossings, so actual request counts are usually much lower. No dollar cost
is estimated - the app has no reliable pricing data.
"""

import math
from dataclasses import dataclass

from invoice_extractor.config import Config


@dataclass(frozen=True)
class FilePlan:
    """Classification-derived per-file counts (computed locally, free)."""
    display_name: str
    text_pages: int
    image_pages: int
    classification: str


@dataclass(frozen=True)
class AttemptEstimate:
    max_attempts: int          # upper bound on application-issued HTTP requests
    assumptions: tuple[str, ...]


def estimate_max_attempts(plans: list[FilePlan], cfg: Config) -> AttemptEstimate:
    """Upper bound = sum over files of (text_chunks x text_models +
    vision_chunks x vision_models), capped per file by
    MAX_MODEL_ATTEMPTS_PER_FILE, times 2 (one repair per model attempt),
    times MAX_RETRIES (transport retries per request).

    Under the direct gateway the "models" are the fixed provider chains
    (Gemini + optional Claude fallback per route).
    """
    assumptions = [
        "Upper bound only - successful models, validation, and cost budgets "
        "stop escalation early, so actual requests are usually lower.",
        f"Every request may be retried up to MAX_RETRIES={cfg.max_retries} "
        "times on transient transport errors.",
        "Each model attempt may include one JSON-repair request (x2).",
    ]
    if cfg.llm_gateway == "openrouter":
        text_models = max(1, len(cfg.openrouter_text_models))
        vision_models = max(1, len(cfg.openrouter_vision_models))
        assumptions.append(
            f"OpenRouter ladders: {text_models} text model(s), "
            f"{vision_models} vision model(s); text chunks of "
            f"<= {cfg.max_text_pages} page(s), vision chunks of "
            f"<= {cfg.max_vision_pages} page(s).")
    else:
        text_models = 2 if cfg.enable_claude_text_fallback else 1
        vision_models = 2  # Gemini primary + Claude fallback (always on)
        assumptions.append("Direct gateway: Gemini primary with Claude fallback.")

    cap = cfg.max_model_attempts_per_file
    if cap is not None:
        assumptions.append(
            f"MAX_MODEL_ATTEMPTS_PER_FILE={cap} caps model attempts per file.")
    else:
        assumptions.append(
            "No MAX_MODEL_ATTEMPTS_PER_FILE cap is configured - consider "
            "setting one before large runs.")
    if cfg.max_cost_usd_per_file is not None or cfg.max_cost_usd_per_run is not None:
        assumptions.append(
            "Configured cost budgets stop further requests once the reported "
            "cost crosses the limit (not reflected in the bound).")

    total_attempts = 0
    for plan in plans:
        text_chunks = math.ceil(plan.text_pages / cfg.max_text_pages) \
            if plan.text_pages else 0
        vision_chunks = math.ceil(plan.image_pages / cfg.max_vision_pages) \
            if plan.image_pages else 0
        model_attempts = text_chunks * text_models + vision_chunks * vision_models
        if cap is not None:
            model_attempts = min(model_attempts, cap)
        total_attempts += model_attempts

    return AttemptEstimate(
        max_attempts=total_attempts * 2 * max(1, cfg.max_retries),
        assumptions=tuple(assumptions),
    )
