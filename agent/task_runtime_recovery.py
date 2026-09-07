"""Bounded provider recovery that never changes operation ownership."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: float
    status: str


def bounded_429_retry(*, attempt: int, retry_after: Any = None, budget: int = 3) -> RetryDecision:
    """Return a bounded delay for a transient 429.

    The caller owns sleeping and must reuse the same request/operation state.
    This helper intentionally has no fallback-model behavior.
    """
    if attempt >= max(0, int(budget)):
        return RetryDecision(False, 0.0, "provider_transient_blocked")
    try:
        header_delay = max(0.0, float(retry_after)) if retry_after is not None else 0.0
    except (TypeError, ValueError):
        header_delay = 0.0
    delay = min(600.0, header_delay) if header_delay else min(60.0, 2.0 ** max(0, attempt))
    return RetryDecision(True, delay, "retry_429")
