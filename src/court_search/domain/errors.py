from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidArgument(DomainError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("INVALID_ARGUMENT", message, details=details, http_status=400)


class UnknownFilter(DomainError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("UNKNOWN_FILTER", message, details=details, http_status=400)


class NotFound(DomainError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("NOT_FOUND", message, details=details, http_status=404)


class VersionUnavailable(DomainError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("VERSION_UNAVAILABLE", message, details=details, http_status=409)


class CursorExpired(DomainError):
    def __init__(self, message: str = "Search cursor expired. Restart the search.") -> None:
        super().__init__("CURSOR_EXPIRED", message, retryable=False, http_status=410)


class CorpusNotReady(DomainError):
    def __init__(
        self,
        message: str = "No active corpus generation is available yet.",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "CORPUS_NOT_READY",
            message,
            retryable=True,
            details=details,
            http_status=503,
        )


class ModelUnavailable(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__("MODEL_UNAVAILABLE", message, retryable=True, http_status=503)


class SearchTimeout(DomainError):
    def __init__(self, message: str = "Search exceeded the request deadline.") -> None:
        super().__init__("SEARCH_TIMEOUT", message, retryable=True, http_status=504)
