import asyncio
import random

from loguru import logger
from playwright.async_api import Page


class HumanBehavior:
    """
    Simulates realistic human interactions on a page to maintain
    a healthy reCAPTCHA score during long automation sessions.

    All movements and interactions are randomized to avoid detectable patterns.
    """

    async def idle_activity(self, page: Page) -> None:
        """
        Perform a random mix of human-like actions: mouse moves, scrolls, pauses.
        Called between reCAPTCHA token generations.
        """
        actions = random.randint(1, 2)
        for _ in range(actions):
            choice = random.random()
            if choice < 0.5:
                await self._random_mouse_move(page)
            elif choice < 0.8:
                await self._random_scroll(page)
            else:
                await self._short_pause()

    async def _random_mouse_move(self, page: Page) -> None:
        """Move mouse to a random position with human-like intermediate steps."""
        try:
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            x = random.randint(100, viewport["width"] - 100)
            y = random.randint(100, viewport["height"] - 100)

            steps = random.randint(5, 15)
            await page.mouse.move(x, y, steps=steps)
            logger.debug(f"Mouse moved to ({x}, {y}) in {steps} steps")

            if random.random() < 0.15:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                x2 = x + random.randint(-80, 80)
                y2 = y + random.randint(-60, 60)
                x2 = max(50, min(viewport["width"] - 50, x2))
                y2 = max(50, min(viewport["height"] - 50, y2))
                await page.mouse.move(x2, y2, steps=random.randint(3, 8))
                logger.debug(f"Mouse drifted to ({x2}, {y2})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Mouse move skipped: {e}")

    async def _random_scroll(self, page: Page) -> None:
        """Scroll the page slightly up or down."""
        try:
            delta_y = random.randint(-200, 200)
            await page.mouse.wheel(0, delta_y)
            logger.debug(f"Scrolled {delta_y:+d}px")
            await asyncio.sleep(random.uniform(0.2, 0.5))

            if random.random() < 0.4:
                await page.mouse.wheel(0, -delta_y // 2)
                logger.debug(f"Scroll correction {-delta_y // 2:+d}px")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Scroll skipped: {e}")

    async def _short_pause(self) -> None:
        """Simulate a brief moment of inactivity."""
        duration = random.uniform(0.5, 2.0)
        logger.debug(f"Idle pause {duration:.2f}s")
        await asyncio.sleep(duration)
