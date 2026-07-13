"""Optional debug-artifact persistence.

DISABLED by default (SAVE_DEBUG_ARTIFACTS=false). When enabled, raw provider
responses that ultimately FAILED extraction (malformed JSON even after
repair, or missing required fields) are written to DEBUG_ARTIFACT_DIR - never
on a successful response. These artifacts MAY CONTAIN CONFIDENTIAL BUSINESS
DATA (full invoice contents) - never enable in shared environments and never
commit the directory (it must stay git-ignored, same as `.env`).
"""

import re
import time
from pathlib import Path

from invoice_extractor.config import Config

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def save_debug_artifact(
    cfg: Config, label: str, *, model: str, reason: str, raw_text: str,
) -> Path | None:
    """Persist one FAILED provider response with enough metadata to debug it.

    `label` already encodes source file + route + provider by the existing
    calling convention (e.g. "invoice1.pdf_gemini_text",
    "invoice1.pdf_claude_vision_c2"); `model` and `reason` are recorded
    explicitly alongside it. `reason` must itself already be safe to log
    (callers pass ExtractionError's message, which never embeds response
    content - see schema.ExtractionError's docstring).
    """
    if not cfg.save_debug_artifacts or raw_text is None:
        return None
    directory = Path(cfg.debug_artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_RE.sub("_", label).strip("_") or "artifact"
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.txt"
    header = f"label: {label}\nmodel: {model}\nreason: {reason}\n{'-' * 40}\n"
    path.write_text(header + raw_text, encoding="utf-8")
    return path
