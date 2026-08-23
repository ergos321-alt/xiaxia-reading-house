"""Private browser-session and Custom GPT Action authentication."""

from __future__ import annotations

import hmac
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import current_app, jsonify, redirect, request, session, url_for


F = TypeVar("F", bound=Callable[..., Any])


def _safe_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def bearer_is_valid() -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and _safe_equal(
        token.strip(), current_app.config.get("ACTION_API_TOKEN")
    )


def browser_is_authenticated() -> bool:
    return session.get("private_access") is True


def api_or_session_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if bearer_is_valid() or browser_is_authenticated():
            return view(*args, **kwargs)
        return jsonify({"error": "unauthorized"}), 401

    return wrapped  # type: ignore[return-value]


def web_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if browser_is_authenticated():
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    return wrapped  # type: ignore[return-value]
