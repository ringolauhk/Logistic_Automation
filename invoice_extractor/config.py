import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    gemini_api_key: str | None
    anthropic_api_key: str | None
    gemini_model: str
    claude_model: str
    render_dpi: int
    # An extracted page counts as "real" text when the average number of
    # alphanumeric characters per page exceeds this threshold.
    text_quality_threshold: int
    # Cap on pages sent to the vision APIs per PDF (cost guard).
    max_vision_pages: int
    max_retries: int


def load_config() -> Config:
    return Config(
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5"),
        render_dpi=int(os.getenv("RENDER_DPI", "200")),
        text_quality_threshold=int(os.getenv("TEXT_QUALITY_THRESHOLD", "20")),
        max_vision_pages=int(os.getenv("MAX_VISION_PAGES", "5")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
    )
