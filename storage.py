"""Server-only access to the private Supabase Storage bucket."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from supabase import Client, create_client


class ObjectStorageError(RuntimeError):
    """Raised when a private Storage operation cannot be completed."""


class ObjectStorageBatchError(ObjectStorageError):
    """A bounded batch failed after some objects may already have uploaded."""

    def __init__(self, message: str, uploaded_paths: list[str]) -> None:
        super().__init__(message)
        self.uploaded_paths = uploaded_paths


class ObjectStorageDeadlineError(ObjectStorageBatchError):
    """The caller's import deadline expired during a bounded asset batch."""


_client: Client | None = None
_supabase_url: str | None = None
_service_role_key: str | None = None
_bucket_name: str | None = None
_client_lock = threading.Lock()


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


def upload_file(object_path: str, file_path: str | Path, media_type: str) -> None:
    """Stream one local file to Storage without materialising it as bytes."""
    try:
        with Path(file_path).open("rb") as source:
            _bucket().upload(
                path=object_path,
                file=source,
                file_options={
                    "content-type": media_type,
                    "cache-control": "86400",
                    "upsert": "false",
                },
            )
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage upload failed") from exc


def upload_many(
    objects: list[tuple[str, bytes, str]], *, max_workers: int = 2
) -> list[str]:
    """Upload distinct required objects with small bounded concurrency.

    Supabase Storage has no multi-object upload endpoint.  Concurrency removes
    needless serial network latency while the caller retains exact rollback
    knowledge if any individual upload fails.
    """
    unique: dict[str, tuple[bytes, str]] = {}
    for path, data, media_type in objects:
        unique.setdefault(path, (data, media_type))
    if not unique:
        return []
    uploaded: list[str] = []
    worker_count = max(1, min(max_workers, len(unique)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(upload_bytes, path, data, media_type): path
            for path, (data, media_type) in unique.items()
        }
        failure: Exception | None = None
        for future in as_completed(futures):
            path = futures[future]
            try:
                future.result()
                uploaded.append(path)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise ObjectStorageBatchError(
                "Supabase Storage batch upload failed", sorted(uploaded)
            ) from failure
    return sorted(uploaded)


def upload_archive_entries(
    archive_path: str | Path,
    objects: list[tuple[str, str, str]],
    *,
    max_workers: int = 2,
    deadline: float | None = None,
) -> list[str]:
    """Extract and upload only selected ZIP entries with bounded memory."""
    unique: dict[str, tuple[str, str]] = {}
    for object_path, entry_path, media_type in objects:
        unique.setdefault(object_path, (entry_path, media_type))
    if not unique:
        return []

    uploaded: list[str] = []
    worker_count = max(1, min(max_workers, len(unique)))
    with tempfile.TemporaryDirectory(prefix="rh-epub-assets-") as temp_dir:
        temp_root = Path(temp_dir)

        def extract_and_upload(
            index: int,
            object_path: str,
            entry_path: str,
            media_type: str,
        ) -> str:
            if deadline is not None and time.perf_counter() >= deadline:
                raise TimeoutError("book import deadline reached")
            staged_path = temp_root / f"asset-{index:06d}"
            with zipfile.ZipFile(archive_path) as archive:
                with archive.open(entry_path) as source:
                    with staged_path.open("wb") as target:
                        shutil.copyfileobj(source, target, length=256 * 1024)
            upload_file(object_path, staged_path, media_type)
            staged_path.unlink(missing_ok=True)
            return object_path

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    extract_and_upload,
                    index,
                    object_path,
                    entry_path,
                    media_type,
                ): object_path
                for index, (object_path, (entry_path, media_type)) in enumerate(
                    unique.items()
                )
            }
            failure: Exception | None = None
            deadline_reached = False
            for future in as_completed(futures):
                try:
                    uploaded.append(future.result())
                except Exception as exc:
                    failure = failure or exc
                    deadline_reached = deadline_reached or isinstance(
                        exc, TimeoutError
                    )
            if failure is not None:
                error_type = (
                    ObjectStorageDeadlineError
                    if deadline_reached
                    else ObjectStorageBatchError
                )
                raise error_type(
                    "Supabase Storage archive upload failed", sorted(uploaded)
                ) from failure
    return sorted(uploaded)


def download_bytes(object_path: str) -> bytes:
    """Download one object from the private bucket on the server."""
    try:
        response = _bucket().download(object_path)
        return bytes(response)
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage download failed") from exc


def create_signed_download_url(object_path: str, expires_in: int) -> str:
    """Create a short-lived browser download URL without exposing credentials."""
    if not 60 <= expires_in <= 300:
        raise ValueError("signed URL TTL must be between 60 and 300 seconds")
    try:
        result = _bucket().create_signed_url(object_path, expires_in)
        if isinstance(result, dict):
            signed_url = (
                result.get("signedURL")
                or result.get("signedUrl")
                or result.get("signed_url")
            )
        else:
            signed_url = getattr(result, "signed_url", None)
        if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
            raise ValueError("Storage did not return a valid signed URL")
        return signed_url
    except ObjectStorageError:
        raise
    except Exception as exc:
        raise ObjectStorageError("Supabase Storage signed URL failed") from exc


def delete_objects(object_paths: list[str]) -> None:
    """Remove objects during rollback cleanup."""
    if not object_paths:
        return
    try:
        # Keep requests comfortably below provider batch limits for image-heavy EPUBs.
        unique_paths = sorted(set(object_paths))
        for start in range(0, len(unique_paths), 100):
            _bucket().remove(unique_paths[start : start + 100])
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
        # Missing/unknown visibility is not safe enough for private books.
        return public is False
    except Exception:
        return False


def object_path_for_asset(
    book_id: UUID,
    asset_path: str,
    is_cover: bool,
    content_sha256: str | None = None,
) -> str:
    """Return the deterministic private object path for one EPUB asset."""
    suffix = PurePosixPath(asset_path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ""
    digest = (
        content_sha256
        if content_sha256 and re.fullmatch(r"[a-f0-9]{64}", content_sha256)
        else hashlib.sha256(asset_path.encode("utf-8")).hexdigest()
    )[:24]
    category = "cover" if is_cover else "assets"
    return f"books/{book_id}/{category}/{digest}{suffix}"


def _bucket():
    return _get_client().storage.from_(_required_bucket_name())


def _get_client() -> Client:
    global _client
    if not _supabase_url or not _service_role_key:
        raise ObjectStorageError("Supabase Storage is not configured")
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = create_client(_supabase_url, _service_role_key)
    return _client


def _required_bucket_name() -> str:
    if not _bucket_name:
        raise ObjectStorageError("SUPABASE_STORAGE_BUCKET is not configured")
    return _bucket_name
