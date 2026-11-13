"""Application settings loaded from environment variables or .env."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings needed to connect the API to MongoDB."""

    mongodb_uri: str
    mongodb_database: str = "smart_parking"
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    @field_validator("mongodb_uri")
    @classmethod
    def validate_mongodb_uri(cls, value: str) -> str:
        """Catch the example URI before PyMongo attempts to resolve it."""
        if "<" in value or ">" in value:
            raise ValueError(
                "MONGODB_URI still has placeholders; paste the connection string "
                "from MongoDB Atlas into backend/.env."
            )
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Create settings once and reuse them throughout the application."""
    return Settings()

