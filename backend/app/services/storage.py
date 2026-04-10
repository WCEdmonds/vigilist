"""Firebase Storage helper functions."""

import datetime
import logging
import os
import tempfile

import firebase_admin
from firebase_admin import storage

from app.config import settings

logger = logging.getLogger(__name__)

# Cached default credentials + email for URL signing. On Cloud Run the default
# credentials are metadata-server tokens that don't carry a private key, so
# blob.generate_signed_url() can't sign locally. We pass the service account
# email + a fresh access token and let google-cloud-storage call the IAM
# signBlob API under the hood. The SA must have
# roles/iam.serviceAccountTokenCreator on itself.
_signing_credentials = None
_signing_sa_email: str | None = None


def _fetch_metadata_sa_email() -> str | None:
    """Ask the GCE/Cloud Run metadata server for the current runtime
    service account email. Returns None if the metadata server is not
    reachable (e.g. running locally)."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            email = resp.read().decode("utf-8").strip()
            return email or None
    except Exception:
        return None


def _get_signing_context() -> tuple[object | None, str | None]:
    global _signing_credentials, _signing_sa_email
    if _signing_credentials is None:
        try:
            from google.auth import default as google_auth_default
            creds, _project = google_auth_default()
            _signing_credentials = creds
            # compute_engine.Credentials initializes service_account_email to
            # the literal string "default", which the IAM signBlob API then
            # rejects. Prefer the VIGILIST_SIGNING_SA_EMAIL override if set,
            # otherwise ask the metadata server for the real email.
            email = os.environ.get("VIGILIST_SIGNING_SA_EMAIL")
            if not email:
                attr = getattr(creds, "service_account_email", None)
                if attr and attr != "default":
                    email = attr
            if not email:
                email = _fetch_metadata_sa_email()
            _signing_sa_email = email
        except Exception:
            logger.exception("Failed to load default credentials for URL signing")
            _signing_credentials = None
            _signing_sa_email = None
    return _signing_credentials, _signing_sa_email


def get_bucket():
    """Get the Firebase Storage bucket."""
    return storage.bucket(settings.firebase_storage_bucket)


def download_file(remote_path: str, local_path: str) -> str:
    """Download a file from Firebase Storage to a local path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    blob.download_to_filename(local_path)
    return local_path


def download_to_temp(remote_path: str, suffix: str = "") -> str:
    """Download a file from Firebase Storage to a temp file. Returns temp path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    blob.download_to_filename(tmp_path)
    return tmp_path


def upload_file(local_path: str, remote_path: str, content_type: str | None = None) -> str:
    """Upload a local file to Firebase Storage. Returns the remote path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    blob.upload_from_filename(local_path, content_type=content_type)
    return remote_path


def list_files(prefix: str) -> list[str]:
    """List all file paths under a prefix in Firebase Storage."""
    bucket = get_bucket()
    blobs = bucket.list_blobs(prefix=prefix)
    return [blob.name for blob in blobs]


def get_download_bytes(remote_path: str) -> bytes:
    """Download a file from Firebase Storage as bytes."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    return blob.download_as_bytes()


def get_signed_url(remote_path: str, expiration_minutes: int = 60) -> str:
    """Generate a signed URL for a file in Firebase Storage.

    On Cloud Run the default credentials are metadata-server access tokens,
    which `blob.generate_signed_url` cannot sign locally. We detect that
    case and delegate signing to the IAM signBlob API by passing the service
    account email + access token."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    expiration = datetime.timedelta(minutes=expiration_minutes)

    try:
        return blob.generate_signed_url(expiration=expiration, method="GET")
    except AttributeError:
        # "you need a private key to sign credentials" — fall back to IAM
        # signBlob using the current runtime credentials.
        pass

    creds, sa_email = _get_signing_context()
    if not creds or not sa_email:
        raise RuntimeError(
            "Cannot sign URL: no service account email available from default credentials"
        )

    # Ensure the token is fresh before we hand it to storage for signing.
    try:
        from google.auth.transport.requests import Request as AuthRequest
        creds.refresh(AuthRequest())
    except Exception:
        logger.exception("Failed to refresh default credentials before signing URL")

    return blob.generate_signed_url(
        version="v4",
        expiration=expiration,
        method="GET",
        service_account_email=sa_email,
        access_token=creds.token,
    )


def file_exists(remote_path: str) -> bool:
    """Check if a file exists in Firebase Storage."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    return blob.exists()
