"""Anti-detection measures for Playwright scraping.

Random delays, human-ish scrolling, stealth measures, and the load-bearing
pieces for long-running scrapes: a token-bucket rate limiter and an
exponential CAPTCHA backoff.
"""

from __future__ import annotations

import asyncio
import random
import time

import structlog
from playwright.async_api import Page

from config import ExtractionConfig

logger = structlog.get_logger(__name__)


async def random_delay(config: ExtractionConfig) -> None:
    """Wait a random delay between configured min and max seconds."""
    delay = random.uniform(config.action_delay_min_s, config.action_delay_max_s)
    logger.debug("random_delay", seconds=round(delay, 2))
    await asyncio.sleep(delay)


class RateLimiter:
    """Async token bucket. Caps the rate of place scrapes globally.

    The pipeline calls `await rate_limiter.acquire()` before each place. With
    rate=5/min, the first 5 calls go through instantly (initial bucket), then
    subsequent calls block until the next token refills (~12s each).
    """

    def __init__(self, rate_per_minute: float) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be > 0")
        self._capacity = max(rate_per_minute, 1.0)
        self._tokens = self._capacity
        self._refill_per_s = rate_per_minute / 60.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last) * self._refill_per_s
            )
            self._last = now
            if self._tokens < 1:
                wait_s = (1 - self._tokens) / self._refill_per_s
                logger.debug("rate_limit_wait", seconds=round(wait_s, 1))
                await asyncio.sleep(wait_s)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1


class CaptchaBackoff:
    """Exponential backoff for CAPTCHA / soft-block detection.

    Call `await backoff.trigger()` when a CAPTCHA is detected. It sleeps for
    progressively longer (base, base*3, base*6, base*12 ...) and gives up
    after `max_attempts` retries — at which point the pipeline should abort
    and let the user resume later.
    """

    def __init__(self, base_seconds: float, max_attempts: int = 4) -> None:
        self._base = base_seconds
        self._max = max_attempts
        self._attempts = 0

    @property
    def exhausted(self) -> bool:
        return self._attempts >= self._max

    async def trigger(self) -> bool:
        """Sleep for the next backoff window. Returns False if exhausted."""
        if self.exhausted:
            return False
        self._attempts += 1
        # 1x, 3x, 6x, 12x — quickly grows past 1 hour to give Google time to forget.
        multiplier = (self._attempts - 1) * 3 if self._attempts > 1 else 1
        wait_s = self._base * max(1, multiplier)
        logger.warning(
            "captcha_backoff",
            attempt=self._attempts,
            max_attempts=self._max,
            sleep_s=round(wait_s, 1),
        )
        await asyncio.sleep(wait_s)
        return True

    def reset(self) -> None:
        """Call after a successful scrape so future CAPTCHAs start at base sleep."""
        self._attempts = 0


async def scroll_review_panel(page: Page, config: ExtractionConfig) -> int:
    """Scroll the review panel using mouse wheel events to trigger lazy loading.

    Returns the total number of reviews currently in the DOM after scrolling.
    """
    before = await page.evaluate(
        "() => document.querySelectorAll('div.jftiEf, div[data-review-id]').length"
    )

    # Human-ish scroll: a few mouse wheel ticks with jitter.
    for _ in range(random.randint(2, 4)):
        delta = random.randint(300, 800)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.3, 0.8))

    # Let Maps paint the next batch.
    await asyncio.sleep(config.scroll_delay_ms / 1000)

    after = await page.evaluate(
        "() => document.querySelectorAll('div.jftiEf, div[data-review-id]').length"
    )
    logger.debug("scroll_complete", before=before, after=after, new=after - before)
    return after


async def wait_for_reviews_loaded(page: Page, timeout_ms: int = 5000) -> bool:
    """Wait for review content to be present in the DOM."""
    try:
        await page.wait_for_selector(
            'div.jftiEf, div[data-review-id]',
            timeout=timeout_ms,
        )
        return True
    except Exception:
        logger.warning("reviews_not_loaded", timeout_ms=timeout_ms)
        return False


async def detect_captcha(page: Page) -> bool:
    """Detect if a CAPTCHA / unusual-traffic interstitial is showing."""
    try:
        content = (await page.content()).lower()
    except Exception:
        return False
    for indicator in (
        "recaptcha",
        "unusual traffic",
        "verify you are human",
        "non sei un robot",
        "traffico insolito",
    ):
        if indicator in content:
            logger.warning("captcha_detected", indicator=indicator)
            return True
    return False
