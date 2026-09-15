"""Tests for the retry helper, using a fake clock so nothing actually sleeps."""

from __future__ import annotations

import contextlib
import logging
import random
import unittest

from lead_monitor.retry import RetryExhaustedError, with_retries

# These tests deliberately provoke failures; the warnings are expected noise.
logging.getLogger("lead_monitor.retry").setLevel(logging.CRITICAL)


class Recorder:
    """Captures sleep durations instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class TransientError(Exception):
    pass


class FatalError(Exception):
    pass


class TestSuccess(unittest.TestCase):
    def test_returns_immediately_when_the_first_attempt_works(self) -> None:
        sleeper = Recorder()
        result = with_retries(lambda: "ok", sleep=sleeper)

        self.assertEqual(result, "ok")
        self.assertEqual(sleeper.delays, [])

    def test_recovers_after_transient_failures(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("not yet")
            return "ok"

        sleeper = Recorder()
        result = with_retries(flaky, attempts=4, retry_on=(TransientError,), sleep=sleeper)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeper.delays), 2)


class TestExhaustion(unittest.TestCase):
    def test_raises_after_the_last_attempt(self) -> None:
        sleeper = Recorder()

        def always_fails() -> None:
            raise TransientError("nope")

        with self.assertRaises(RetryExhaustedError):
            with_retries(always_fails, attempts=3, retry_on=(TransientError,), sleep=sleeper)

        # Three attempts means two waits: it must not sleep after the last try.
        self.assertEqual(len(sleeper.delays), 2)

    def test_preserves_the_original_error_as_the_cause(self) -> None:
        def always_fails() -> None:
            raise TransientError("the real reason")

        with self.assertRaises(RetryExhaustedError) as ctx:
            with_retries(always_fails, attempts=2, retry_on=(TransientError,), sleep=Recorder())

        self.assertIsInstance(ctx.exception.__cause__, TransientError)
        self.assertEqual(str(ctx.exception.__cause__), "the real reason")

    def test_single_attempt_never_sleeps(self) -> None:
        sleeper = Recorder()
        with self.assertRaises(RetryExhaustedError):
            with_retries(
                lambda: (_ for _ in ()).throw(TransientError()),
                attempts=1,
                retry_on=(TransientError,),
                sleep=sleeper,
            )
        self.assertEqual(sleeper.delays, [])


class TestSelectivity(unittest.TestCase):
    def test_unlisted_exceptions_propagate_immediately(self) -> None:
        """A 401 or a parse error will not fix itself; retrying only delays it."""
        sleeper = Recorder()
        calls = {"n": 0}

        def fails_fatally() -> None:
            calls["n"] += 1
            raise FatalError("bad credentials")

        with self.assertRaises(FatalError):
            with_retries(fails_fatally, attempts=5, retry_on=(TransientError,), sleep=sleeper)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleeper.delays, [])


class TestBackoff(unittest.TestCase):
    def test_delays_stay_within_the_growing_jitter_window(self) -> None:
        sleeper = Recorder()

        with self.assertRaises(RetryExhaustedError):
            with_retries(
                lambda: (_ for _ in ()).throw(TransientError()),
                attempts=5,
                base_delay=1.0,
                max_delay=30.0,
                retry_on=(TransientError,),
                sleep=sleeper,
                rng=random.Random(1234),
            )

        # Full jitter: each delay lies in [0, min(base * 2**n, max_delay)].
        for index, delay in enumerate(sleeper.delays):
            ceiling = min(1.0 * (2**index), 30.0)
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, ceiling)

    def test_respects_the_ceiling(self) -> None:
        sleeper = Recorder()

        with self.assertRaises(RetryExhaustedError):
            with_retries(
                lambda: (_ for _ in ()).throw(TransientError()),
                attempts=8,
                base_delay=1.0,
                max_delay=5.0,
                retry_on=(TransientError,),
                sleep=sleeper,
                rng=random.Random(7),
            )

        self.assertTrue(all(delay <= 5.0 for delay in sleeper.delays))

    def test_jitter_desynchronises_callers(self) -> None:
        """Identical failures must not produce identical retry schedules."""

        def schedule(seed: int) -> list[float]:
            sleeper = Recorder()
            with contextlib.suppress(RetryExhaustedError):
                with_retries(
                    lambda: (_ for _ in ()).throw(TransientError()),
                    attempts=5,
                    retry_on=(TransientError,),
                    sleep=sleeper,
                    rng=random.Random(seed),
                )
            return sleeper.delays

        self.assertNotEqual(schedule(1), schedule(2))


class TestValidation(unittest.TestCase):
    def test_rejects_zero_attempts(self) -> None:
        with self.assertRaises(ValueError):
            with_retries(lambda: "ok", attempts=0)


if __name__ == "__main__":
    unittest.main()
