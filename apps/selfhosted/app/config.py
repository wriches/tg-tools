"""Application configuration, loaded from the repo-root .env / environment."""
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/selfhosted/app/config.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TG_", env_file=str(REPO_ROOT / ".env"), extra="ignore"
    )

    api_id: int
    api_hash: str
    secret_key: str = "change-me-to-a-long-random-string"
    db_path: str = "data/tg_tools.db"
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def resolved_db_path(self) -> str:
        p = Path(self.db_path)
        return str(p if p.is_absolute() else REPO_ROOT / p)


_settings: Settings | None = None


class ConfigError(RuntimeError):
    """Raised with an actionable message when required settings are missing."""


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        try:
            _settings = Settings()  # type: ignore[call-arg]
        except ValidationError as exc:
            missing = [
                f"TG_{e['loc'][0].upper()}"
                for e in exc.errors()
                if e["type"] == "missing"
            ]
            env_path = REPO_ROOT / ".env"
            hint = (
                f"Missing required setting(s): {', '.join(missing) or 'see below'}.\n"
                f"  1. cp .env.example .env   (creates {env_path})\n"
                "  2. Set TG_API_ID and TG_API_HASH from https://my.telegram.org "
                "(API development tools)\n"
                "  3. Set TG_SECRET_KEY to a long random string\n"
                if missing
                else str(exc)
            )
            raise ConfigError(hint) from None
    return _settings
