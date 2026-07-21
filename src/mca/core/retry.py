"""Retry and backoff, dependency-free.

Design decision — no tenacity
------------------------------
``tenacity`` was listed as a dependency when this project only had scaffolding.
Now that real network code needs retry logic, it turns out the actual
requirement — exponential backoff with jitter, a cap, and a distinction between
"try again" and "stop now" — is about 40 lines, and every module that needs it
(RPC calls, Jupiter calls, Redis stream consumption, websocket reconnects) can
share one dependency-free implementation instead of pulling in a general-purpose
library. This has been removed from ``pyproject.toml`` accordingly.

Design decision — retryable is opt-in, not opt-out
-----------------------------------------------------
``retry_async`` retries only exceptions in the ``retryable`` tuple. Everything
else propagates immediately. This matters concretely in execution: "insufficient
funds" and "honeypot: sell simulation reverted" are not transient — retrying
them three times with backoff just burns 3-6 seconds while the real problem
(no money, or a scam) sits there unchanged. Only genuinely transient failures
(timeouts, connection resets, 429/5xx, blockhash expiry) should ever be retried.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class RetryConfig:
    retries: int = 3                 # attempts *after* the first, so 3 = 4 tries total
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    factor: float = 2.0
    jitter: float = 0.2              # fraction of the delay randomised, both directions


def backoff_delays(cfg: RetryConfig) -> list[float]:
    """Pure delay-sequence calculator, seeded for reproducibility in tests.

    Returns the *unjittered* sequence: callers that need determinism (tests,
    documentation) use this directly; ``retry_async`` applies jitter itself at
    call time so concurrent callers do not retry in lockstep and hammer the
    same endpoint on the same tick (thundering herd).
    """
    out = []
    delay = cfg.base_delay_s
    for _ in range(cfg.retries):
        out.append(min(delay, cfg.max_delay_s))
        delay *= cfg.factor
    return out


def _jittered(delay: float, jitter: float, rng: random.Random) -> float:
    if jitter <= 0:
        return delay
    spread = delay * jitter
    return max(0.0, delay + rng.uniform(-spread, spread))


class RetriesExhausted(Exception):
    """Raised when every attempt failed. Wraps the last underlying exception."""

    def __init__(self, attempts: int, last: BaseException) -> None:
        super().__init__(f"{attempts} attempts failed, last error: {last!r}")
        self.attempts = attempts
        self.last = last


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    cfg: RetryConfig | None = None,
    retryable: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn()`` (a zero-arg async callable) with retry on ``retryable``.

    ``on_retry(attempt, exc, delay_s)`` fires before each sleep — wire it to a
    logger or a metrics counter. Non-``retryable`` exceptions propagate on the
    first attempt with no delay.
    """
    cfg = cfg or RetryConfig()
    rng = rng or random.Random()
    delays = backoff_delays(cfg)

    last_exc: BaseException | None = None
    for attempt in range(cfg.retries + 1):
        try:
            return await fn()
        except retryable as exc:
            last_exc = exc
            if attempt >= cfg.retries:
                break
            delay = _jittered(delays[attempt], cfg.jitter, rng)
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            else:
                log.warning("retry %d/%d after %.2fs: %r",
                           attempt + 1, cfg.retries, delay, exc)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise RetriesExhausted(cfg.retries + 1, last_exc)


def retry_sync(
    fn: Callable[[], T],
    *,
    cfg: RetryConfig | None = None,
    retryable: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Synchronous twin of :func:`retry_async`, for non-asyncio call sites
    (CLI scripts, the scheduler's blocking jobs)."""
    cfg = cfg or RetryConfig()
    rng = rng or random.Random()
    delays = backoff_delays(cfg)

    last_exc: BaseException | None = None
    for attempt in range(cfg.retries + 1):
        try:
            return fn()
        except retryable as exc:
            last_exc = exc
            if attempt >= cfg.retries:
                break
            delay = _jittered(delays[attempt], cfg.jitter, rng)
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            else:
                log.warning("retry %d/%d after %.2fs: %r",
                           attempt + 1, cfg.retries, delay, exc)
            time.sleep(delay)
    assert last_exc is not None
    raise RetriesExhausted(cfg.retries + 1, last_exc)
