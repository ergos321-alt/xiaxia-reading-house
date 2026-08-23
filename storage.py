"""Server-only access to the private Supabase Storage bucket."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from supabase import Client, create_client


class ObjectStorageError(RuntimeError):
    """Raised when a private Storage operation cannot be completed."""


_client: Client | None = None
_supabase_url: str | None = None
_service_role_key: str | None = None
_bucket_name: str | None = None


def init_app(app: Any) -> None:
    """Record server-side Storage settings without connecting at import time."""
    global _client, _supabase_url, _service_role_key, _bucket_name
    new_url = app.config.get("SUPABASE_URL") or None
    new_key = app.config.get("SUPABASE_SERVICE_ROLE_KEY") or None
    new_bucket = app.config.get("SUPABASE_STORAGE_BUCKET") or None
    if (new_url, new_key, new_bucket) != (
        _supabase_url,
        _service_role_key,
        _bucket_name,
    ):
        _client = None
    _supabase_url = new_url
    _service_role_key = new_key
    _bucket_name = new_bucket


def upload_bytes(object_path: str, data: bytes, media_type: str) -> None:
    """Upload one new object; existing paths are never overwritten silently."""
    try:
        _bucket().upload(
            path=object_path,
            file=data,
            file_options={
                "content-type": media_type,
                "cache-control": "86400",
                "upsert": "false",
            },
        )
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage upload failed") from exc


def download_bytes(object_path: str) -> bytes:
    """Download one object from the private bucket on the server."""
    try:
        response = _bucket().download(object_path)
        return bytes(response)
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage download failed") from exc


def delete_objects(object_paths: list[str]) -> None:
    """Remove objects during rollback cleanup."""
    if not object_paths:
        return
    try:
        _bucket().remove(object_paths)
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage cleanup failed") from exc


def ping() -> bool:
    """Confirm that the configured bucket exists and is private."""
    try:
        bucket = _get_client().storage.get_bucket(_required_bucket_name())
        public = (
            bucket.get("public")
            if isinstance(bucket, dict)
            else getattr(bucket, "public", None)
        )
        return public is not True
    except Exception:
        return False


def object_path_for_asset(book_id: UUID, asset_path: str, is_cover: bool) -> str:
    """Return the deterministic private object path for one EPUB asset."""
    suffix = PurePosixPath(asset_path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ""
    digest = hashlib.sha256(asset_path.encode("utf-8")).hexdigest()[:24]
    category = "cover" if is_cover else "assets"
    return f"books/{book_id}/{category}/{digest}{suffix}"


def _bucket():
    return _get_client().storage.from_(_required_bucket_name())


def _get_client() -> Client:
    global _client
    if not _supabase_url or not _service_role_key:
        raise ObjectStorageError("Supabase Storage is not configured")
    if _client is None:
        _client = create_client(_supabase_url, _service_role_key)
    return _client


def _required_bucket_name() -> str:
    if not _bucket_name:
        raise ObjectStorageError("SUPABASE_STORAGE_BUCKET is not configured")
    return _bucket_name
