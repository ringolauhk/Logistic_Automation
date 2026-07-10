"""Optional debug-artifact persistence.

DISABLED by default (SAVE_DEBUG_ARTIFACTS=false). When enabled, raw provider
responses are written to DEBUG_ARTIFACT_DIR. These artifacts MAY CONTAIN
CONFIDENTIAL BUSINESS DATA (full invoice contents) - never enable in shared
environments and never commit the directory.
"""

import re
import time
from pathlib import Path

from invoice_extractor.config import Config

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def save_debug_artifact(cfg: Config, label: str, content: str) -> Path | None:
    if not cfg.save_debug_artifacts or content is None:
        return None
    directory = Path(cfg.debug_artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_RE.sub("_", label).strip("_") or "artifact"
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.txt"
    path.write_text(content, encoding="utf-8")
    return path
