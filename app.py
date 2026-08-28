"""Application factory and private web pages for Xiaxia Reading House V1."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge

import database
import storage
from annotations import annotations_bp
from auth import web_required
from reading import reading_bp
from management import management_bp
from memories import memories_bp
from reader_engine_poc import reader_engine_poc_bp


REQUIRED_SETTINGS = (
    "DATABASE_URL",
    "PRIVATE_ACCESS_PASSWORD",
    "ACTION_API_TOKEN",
    "FLASK_SECRET_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_STORAGE_BUCKET",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "50"))
    app.config.from_mapping(
        DATABASE_URL=os.getenv("DATABASE_URL", ""),
        PRIVATE_ACCESS_PASSWORD=os.getenv("PRIVATE_ACCESS_PASSWORD", ""),
        ACTION_API_TOKEN=os.getenv("ACTION_API_TOKEN", ""),
        SUPABASE_URL=os.getenv("SUPABASE_URL", ""),
        SUPABASE_SERVICE_ROLE_KEY=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        SUPABASE_STORAGE_BUCKET=os.getenv("SUPABASE_STORAGE_BUCKET", ""),
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or None,
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        MAX_UPLOAD_MB=max_upload_mb,
        DB_POOL_MIN=int(os.getenv("DB_POOL_MIN", "1")),
        DB_POOL_MAX=int(os.getenv("DB_POOL_MAX", "2")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=_env_bool("COOKIE_SECURE", True),
        READER_ENGINE_POC_ENABLED=_env_bool("READER_ENGINE_POC_ENABLED", False),
        READER_ENGINE_ENABLED=_env_bool("READER_ENGINE_ENABLED", False),
        READER_SOURCE_SIGNED_URL_TTL=int(
            os.getenv(
                "READER_SOURCE_SIGNED_URL_TTL",
                os.getenv("READER_ENGINE_POC_SIGNED_URL_TTL", "180"),
            )
        ),
        READER_ENGINE_POC_SIGNED_URL_TTL=int(
            os.getenv("READER_ENGINE_POC_SIGNED_URL_TTL", "180")
        ),
    )
    if test_config:
        app.config.update(test_config)

    database.init_app(app)
    storage.init_app(app)
    app.register_blueprint(reading_bp)
    app.register_blueprint(annotations_bp)
    app.register_blueprint(management_bp)
    app.register_blueprint(memories_bp)
    app.register_blueprint(reader_engine_poc_bp)

    @app.before_request
    def require_complete_configuration():
        if request.endpoint in {"health", "static"}:
            return None
        missing = [name for name in REQUIRED_SETTINGS if not _setting(app, name)]
        if missing:
            return (
                jsonify(
                    {
                        "error": "service_not_configured",
                        "missing_environment_variables": missing,
                    }
                ),
                503,
            )
        return None

    @app.get("/health")
    def health():
        missing = [name for name in REQUIRED_SETTINGS if not _setting(app, name)]
        if missing:
            return jsonify(
                {"status": "configuration_required", "missing": missing}
            ), 503
        if not database.ping():
            return jsonify({"status": "database_unavailable"}), 503
        if not storage.ping():
            return jsonify({"status": "storage_unavailable_or_bucket_public"}), 503
        return jsonify({"status": "ok"})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        next_url = request.values.get("next", "")
        if request.method == "POST":
            supplied = request.form.get("password", "")
            expected = app.config["PRIVATE_ACCESS_PASSWORD"]
            import hmac

            if hmac.compare_digest(supplied.encode(), expected.encode()):
                session.clear()
                session["private_access"] = True
                return redirect(_safe_next_url(next_url) or url_for("library_page"))
            error = "密码不正确"
        return render_template("login.html", error=error, next_url=next_url)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return redirect(url_for("library_page"))

    @app.get("/library")
    @web_required
    def library_page():
        return render_template(
            "library.html", max_upload_mb=app.config["MAX_UPLOAD_MB"]
        )

    @app.get("/reader/<uuid:book_id>")
    @web_required
    def reader_page(book_id):
        response = make_response(
            render_template(
                "reader.html",
                book_id=str(book_id),
                reader_engine_enabled=app.config["READER_ENGINE_ENABLED"],
            )
        )
        supabase = urlsplit(app.config.get("SUPABASE_URL", ""))
        storage_origin = (
            f"{supabase.scheme}://{supabase.netloc}"
            if supabase.scheme == "https" and supabase.netloc
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
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/reader/<uuid:book_id>/annotations")
    @web_required
    def annotations_overview_page(book_id):
        return render_template("annotations_overview.html", book_id=str(book_id))

    @app.get("/annotations/manage")
    @web_required
    def annotations_management_page():
        return render_template("annotations_management.html")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return (
            jsonify(
                {
                    "error": "file_too_large",
                    "max_upload_mb": app.config["MAX_UPLOAD_MB"],
                }
            ),
            413,
        )

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found"}), 404
        return render_template("error.html", message="没有找到这个页面。"), 404

    @app.errorhandler(500)
    def server_error(_error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal_server_error"}), 500
        return render_template("error.html", message="小屋暂时出了点问题。"), 500

    return app


def _setting(app: Flask, env_name: str):
    if env_name == "FLASK_SECRET_KEY":
        return app.config.get("SECRET_KEY")
    return app.config.get(env_name)


def _safe_next_url(candidate: str) -> str | None:
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith("/"):
        return None
    return candidate


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
