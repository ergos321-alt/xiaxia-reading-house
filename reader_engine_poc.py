"""Isolated, default-off foliate-js engine POC endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    render_template,
)

import database as db
import storage as object_storage
from auth import web_api_required, web_required


reader_engine_poc_bp = Blueprint("reader_engine_poc", __name__)


@reader_engine_poc_bp.before_request
def require_poc_feature_flag():
    if not current_app.config.get("READER_ENGINE_POC_ENABLED", False):
        abort(404)


@reader_engine_poc_bp.get("/reader-engine-poc")
@reader_engine_poc_bp.get("/reader-engine-poc/<uuid:book_id>")
@web_required
def poc_page(book_id=None):
    response = make_response(
        render_template(
            "reader_engine_poc.html",
            book_id=str(book_id) if book_id else "",
        )
    )
    return _add_poc_security_headers(response)


@reader_engine_poc_bp.get(
    "/api/reader-engine-poc/books/<uuid:book_id>/source"
)
@web_api_required
def poc_book_source(book_id):
    book = db.fetch_one(
        """
        select id, title, format, source_filename, source_object_path
        from books
        where id = %s
        """,
        (book_id,),
    )
    if not book:
        return jsonify({"error": "book_not_found"}), 404
    if str(book.get("format", "")).lower() != "epub":
        return jsonify({"error": "poc_source_not_epub"}), 422

    ttl = int(current_app.config["READER_ENGINE_POC_SIGNED_URL_TTL"])
    if not 60 <= ttl <= 300:
        current_app.logger.error(
            "reader_engine_poc invalid signed URL TTL", extra={"ttl": ttl}
        )
        return jsonify({"error": "poc_configuration_invalid"}), 503
    try:
        signed_url = object_storage.create_signed_download_url(
            book["source_object_path"], ttl
        )
    except object_storage.ObjectStorageError:
        current_app.logger.exception(
            "reader_engine_poc signed source creation failed",
            extra={"book_id": str(book_id)},
        )
        return jsonify({"error": "poc_source_access_failed"}), 502

    response = jsonify(
        {
            "book": {
                "id": str(book["id"]),
                "title": book["title"],
                "filename": book["source_filename"],
            },
            "signed_url": signed_url,
            "expires_in": ttl,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=ttl)
            ).isoformat(),
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _add_poc_security_headers(response):
    supabase_url = current_app.config.get("SUPABASE_URL", "")
    parsed = urlsplit(supabase_url)
    storage_origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme == "https" and parsed.netloc
        else ""
    )
    connect_sources = "'self' blob:"
    if storage_origin:
        connect_sources += f" {storage_origin}"
    response.headers["Content-Security-Policy"] = "; ".join(
        (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            "frame-ancestors 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline' blob:",
            "img-src 'self' data: blob:",
            "font-src 'self' data: blob:",
            f"connect-src {connect_sources}",
            "frame-src blob:",
            "worker-src 'none'",
            "form-action 'self'",
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return response
