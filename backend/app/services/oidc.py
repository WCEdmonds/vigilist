"""Verify OIDC tokens attached to Cloud Tasks HTTP requests.

Cloud Tasks signs each outgoing request with an OIDC token issued for a
configured service account. Cloud Run, when --allow-unauthenticated, does
NOT validate that token automatically — we must verify it ourselves before
trusting the request.
"""

import logging

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import settings

logger = logging.getLogger(__name__)

_transport = google_requests.Request()


async def verify_cloud_tasks_request(request: Request) -> None:
    """FastAPI dependency that rejects unless the request carries a valid
    OIDC token from the configured Cloud Tasks service account.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth[7:]
    try:
        claims = id_token.verify_oauth2_token(
            token,
            _transport,
            audience=settings.cloud_run_service_url,
        )
    except Exception as e:
        logger.warning("OIDC token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid OIDC token")

    email = claims.get("email")
    if (
        settings.cloud_tasks_service_account
        and email != settings.cloud_tasks_service_account
    ):
        logger.warning("OIDC token email mismatch: got %s", email)
        raise HTTPException(status_code=403, detail="OIDC token email mismatch")
