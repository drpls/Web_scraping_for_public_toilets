"""Authentication and persistent Chrome profile management.

Handles the EEA auth wall by using a persistent Chrome profile.
Based on the revcli approach: user logs in once, session is reused.
"""

from __future__ import annotations

import structlog
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from config import ExtractionConfig

logger = structlog.get_logger(__name__)


class BrowserManager:
    """Manages Playwright browser with persistent profile for EEA auth."""

    def __init__(self, config: ExtractionConfig) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def launch(self) -> BrowserContext:
        """Launch browser with persistent profile and return context."""
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.config.profile_dir),
            headless=self.config.headless,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            locale="en-GB",
            timezone_id="Europe/Rome",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            # Stealth: disable webdriver flag
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )

        # Inject stealth script to hide automation
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-GB', 'en', 'it']
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
        """)

        logger.info(
            "browser_launched",
            profile_dir=str(self.config.profile_dir),
            headless=self.config.headless,
        )
        return self._context

    async def close(self) -> None:
        """Close browser and playwright."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("browser_closed")

    async def __aenter__(self) -> BrowserContext:
        return await self.launch()

    async def __aexit__(self, *args: object) -> None:
        await self.close()


async def handle_cookie_consent(page: object) -> bool:
    """Handle cookie consent banners if present.

    Returns True if consent was handled.
    """
    from gmaps_selectors import NavigationSelectors as Nav

    # Try English button first, then Italian
    for selector in [Nav.COOKIE_ACCEPT_BUTTON, Nav.COOKIE_ACCEPT_ITALIAN]:
        try:
            btn = await page.query_selector(selector)  # type: ignore[union-attr]
            if btn:
                await btn.click()
                logger.info("cookie_consent_accepted", selector=selector)
                return True
        except Exception:
            continue

    logger.debug("no_cookie_consent_found")
    return False


async def check_eea_auth_wall(page: object) -> bool:
    """Check if EEA auth wall is blocking review access.

    Returns True if auth wall is detected.
    """
    from gmaps_selectors import PlaceSelectors as Place

    try:
        wall = await page.query_selector(Place.LIMITED_VIEW)  # type: ignore[union-attr]
        if wall:
            text = await wall.inner_text()  # type: ignore[union-attr]
            if "sign in" in text.lower() or "accedi" in text.lower():
                logger.warning("eea_auth_wall_detected")
                return True
    except Exception:
        pass

    return False


async def dismiss_sign_in_prompt(page: object) -> None:
    """Dismiss Google sign-in prompts if they appear."""
    from gmaps_selectors import NavigationSelectors as Nav

    try:
        btn = await page.query_selector(Nav.DISMISS_SIGN_IN)  # type: ignore[union-attr]
        if btn:
            await btn.click()
            logger.info("sign_in_prompt_dismissed")
    except Exception:
        pass