from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vigilist:vigilist_dev@localhost:5432/vigilist"
    # Firebase
    firebase_project_id: str = ""
    firebase_storage_bucket: str = ""
    # Local file storage root for converted images (will be replaced by Firebase Storage in a later plan)
    storage_root: str = "./storage"
    # CORS origins (Firebase Hosting domain added for prod)
    cors_origins: list[str] = ["http://localhost:5173", "https://ediscover.web.app", "https://ediscover.firebaseapp.com"]
    # Anthropic API key for AI features
    anthropic_api_key: str = ""
    # Resend email
    resend_api_key: str = ""
    resend_from_email: str = "Vigilist <noreply@qndary.com>"
    app_url: str = "https://ediscover.web.app"

    model_config = {"env_prefix": "VIGILIST_", "env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
