"""Exponential backoff with full jitter, on the standard library only.

A third-party retry library would do the same job, but the behaviour needed here
fits in sixty lines and keeping the dependency list short matters for something
that runs unattended every ten minutes: fewer packages means fewer surprise
breakages on a scheduled run nobody is watching.

Full jitter (sleep uniformly in ``[0, backoff]`` rather than exactly ``backoff``)
is the deliberate choice. The portal is a shared system and several clubs may be
polling it; synchronised retries after a blip would arrive as a thundering herd.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExhaustedError(RuntimeError):
    """Every attempt failed. Carries the last error as ``__cause__``."""


def with_retries(
    operation: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    description: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``operation``, retrying transient failures with jittered backoff.

    Raises :class:`RetryExhaustedError` once ``attempts`` have been used, chaining the
    final exception so the original traceback is not lost. Exceptions outside
    ``retry_on`` propagate immediately — a 401 or a malformed response will not
    fix itself, and retrying it only delays a real failure.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    retryable = tuple(retry_on)
    randomiser = rng or random.Random()
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retryable as error:
            last_error = error
            if attempt == attempts:
                break

            backoff = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay = randomiser.uniform(0, backoff)
            logger.warning(
                "Attempt failed, retrying",
                extra={
                    "operation": description,
                    "attempt": attempt,
                    "of": attempts,
                    "delay_seconds": round(delay, 2),
                    "error": str(error),
                },
            )
            sleep(delay)

    raise RetryExhaustedError(f"{description} failed after {attempts} attempts") from last_error
