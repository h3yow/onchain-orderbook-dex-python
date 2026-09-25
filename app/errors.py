"""Domain errors with distinguishable codes."""
from __future__ import annotations

from app.models import ErrorCode


class DexError(Exception):
    """Business error carrying a stable, distinguishable error code."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code.value, "detail": self.message}
