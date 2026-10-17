import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    database_url: str   # app state (Postgres); migrated by `alembic upgrade head`
    usda_db_path: Path  # local, read-only USDA store built by scripts/build_nutrition_db.py
    shelfatlas_api_key: str | None
    # Provider-neutral LLM config: point at any OpenAI-compatible endpoint.
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    # Optional cheaper/different model for display translation; empty = reuse the LLM_* above.
    translate_base_url: str
    translate_model: str
    translate_api_key: str | None


def load_config() -> Config:
    """Read settings from environment (and a local .env, if present)."""
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return Config(
        database_url=database_url,
        usda_db_path=Path(os.environ.get("USDA_DB_PATH", "data/usda.db")),
        shelfatlas_api_key=os.environ.get("SHELFATLAS_API_KEY"),
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        llm_model=os.environ.get("LLM_MODEL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY"),
        translate_base_url=os.environ.get("TRANSLATE_BASE_URL", ""),
        translate_model=os.environ.get("TRANSLATE_MODEL", ""),
        translate_api_key=os.environ.get("TRANSLATE_API_KEY"),
    )
