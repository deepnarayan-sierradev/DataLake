"""
Structured error types for the control-plane API.

Every error raised by a control-plane handler function must be one of these
types (or an unexpected exception, which the top-level dispatcher in
control_plane_handler.py converts to a generic 500). This guarantees the
Lambda always returns a caller-safe JSON error body — never a raw stack trace
or exception message (OWASP A09 — see module docstring in
control_plane_handler.py for the platform-wide convention this follows).
"""

from __future__ import annotations


class ApiError(Exception):
    """
    Base class for control-plane API errors.

    `status_code` is the HTTP status the top-level Lambda dispatcher should
    return. `message` is a caller-safe description — it must never contain
    raw exception text, stack traces, or internal record field values.
    """

    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationFailedError(ApiError):
    """Request input failed Pydantic or identifier-policy validation."""

    status_code = 400


class AuthenticationError(ApiError):
    """The request is missing a valid authenticated identity context."""

    status_code = 401


class AuthorizationError(ApiError):
    """The authenticated identity is not permitted to perform this action."""

    status_code = 403


class NotFoundError(ApiError):
    """The requested resource does not exist (or is not visible to this tenant)."""

    status_code = 404


class ConflictError(ApiError):
    """The requested resource already exists."""

    status_code = 409


class ScopeStoreUnavailableApiError(ApiError):
    """The scope store could not be read, so no row-level authorisation answer can be trusted."""

    # 503 rather than 500: retrying is the correct client behaviour, and unlike a scope *denial*
    # this is a transient fault that will resolve without a grant change.
    status_code = 503
