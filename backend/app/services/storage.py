"""Firebase Storage helper functions."""

import datetime
import os
import tempfile

import firebase_admin
from firebase_admin import storage

from app.config import settings


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
    """Generate a signed URL for a file in Firebase Storage."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    url = blob.generate_signed_url(
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="GET",
    )
    return url


def file_exists(remote_path: str) -> bool:
    """Check if a file exists in Firebase Storage."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    return blob.exists()


def delete_prefix(prefix: str) -> int:
    """Delete all files under a prefix in Firebase Storage.

    Used to clean up a production's storage when the production is
    deleted. Returns the number of blobs deleted.
    """
    bucket = get_bucket()
    deleted = 0
    for blob in bucket.list_blobs(prefix=prefix):
        try:
            blob.delete()
            deleted += 1
        except Exception:
            pass
    return deleted
