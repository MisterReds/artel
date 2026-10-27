"""Helpers both codecs use."""

from __future__ import annotations

from ...contracts import ErrorClass


def classify(text: str) -> ErrorClass:
    """Map a CLI failure text (the Outcome's error) to an ErrorClass: provider limit, setup/auth, or unknown."""
    from ...failures import classify as kind

    k = kind(text)
    return (ErrorClass("rate_limit", True, None) if k == "limit" else
            ErrorClass("auth", False, None) if k == "setup" else ErrorClass("unknown", False, None))
