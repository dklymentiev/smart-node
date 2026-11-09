"""Categorizer configuration from environment variables."""

import os


def is_categorizer_enabled() -> bool:
    """Master switch for categorizer. Opt-in, disabled by default."""
    return os.getenv("CATEGORIZER_ENABLED", "false").lower() in ("true", "1", "yes")


def get_llm_api_url() -> str:
    """OpenAI-compatible API URL. Empty = embedding-only mode."""
    return os.getenv("LLM_API_URL", "")


def get_llm_api_key() -> str:
    """API key for LLM provider."""
    return os.getenv("LLM_API_KEY", "")


def get_llm_model() -> str:
    """Model name for LLM classification (e.g. google/gemini-2.0-flash)."""
    return os.getenv("LLM_MODEL", "")


def get_category_threshold() -> float:
    """Minimum cosine similarity for category match."""
    return float(os.getenv("CATEGORIZER_THRESHOLD", "0.55"))


def get_subcategory_threshold() -> float:
    """Minimum cosine similarity for subcategory match."""
    return float(os.getenv("CATEGORIZER_SUB_THRESHOLD", "0.50"))


def get_llm_timeout() -> int:
    """LLM request timeout in seconds."""
    return int(os.getenv("CATEGORIZER_LLM_TIMEOUT", "15"))


def is_llm_available() -> bool:
    """Check if LLM mode is configured (URL + key + model all set)."""
    return bool(get_llm_api_url() and get_llm_api_key() and get_llm_model())
