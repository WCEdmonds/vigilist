from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://descubre:descubre_dev@localhost:5432/descubre"
    # Shared auth credentials
    auth_username: str = "admin"
    auth_password: str = "descubre2026"
    secret_key: str = "change-me-in-production"
    # Local file storage root for converted images
    storage_root: str = "./storage"
    # CORS origins
    cors_origins: list[str] = ["http://localhost:5173"]
    # Anthropic API key for AI features (title generation, etc.)
    anthropic_api_key: str = ""

    model_config = {"env_prefix": "DESCUBRE_"}


settings = Settings()
