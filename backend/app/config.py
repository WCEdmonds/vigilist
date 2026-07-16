from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vigilist:vigilist_dev@localhost:5432/vigilist"
    # Firebase
    firebase_project_id: str = ""
    firebase_storage_bucket: str = ""
    # Local file storage root for converted images (will be replaced by Firebase Storage in a later plan)
    storage_root: str = "./storage"
    # CORS origins (Firebase Hosting domain added for prod)
    cors_origins: list[str] = [
        "http://localhost:5173",
        "https://ediscover.web.app",
        "https://ediscover.firebaseapp.com",
        "https://vigilist.co",
        "https://www.vigilist.co",
    ]
    # Regex for additional allowed origins — accepts vigilist.co and any
    # *.vigilist.co subdomain (app.vigilist.co, staging.app.vigilist.co, ...)
    # over https. Starlette matches this with fullmatch, so lookalike domains
    # (evilvigilist.co, vigilist.co.attacker.com) are rejected.
    cors_origin_regex: str = r"https://([a-z0-9-]+\.)*vigilist\.co"
    # Anthropic API key for AI features
    anthropic_api_key: str = ""
    # Voyage AI API key for embeddings (semantic search, clustering,
    # near-duplicate detection). Unset = those features degrade gracefully
    # (semantic search falls back to full-text).
    voyage_api_key: str = ""
    # Resend email
    resend_api_key: str = ""
    resend_from_email: str = "Vigilist <noreply@qndary.com>"
    app_url: str = "https://ediscover.web.app"
    # Cloud Tasks / GCP — used to fan out ingest work across separate
    # Cloud Run invocations so long-running ingests can't be killed by
    # container scale-down. When these are unset, ingest falls back to
    # an in-process BackgroundTask (fine for local dev, unreliable on
    # Cloud Run for long jobs).
    gcp_project_id: str = ""
    gcp_location: str = "us-central1"
    cloud_tasks_queue: str = "vigilist-ingest"
    cloud_run_service_url: str = ""
    cloud_tasks_service_account: str = ""

    model_config = {"env_prefix": "VIGILIST_", "env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
