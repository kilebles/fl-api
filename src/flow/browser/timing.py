import asyncio
import random


class HumanTiming:
    """
    Delays that mimic real user behavior observed in browser network logs.

    Intervals between generations: mean ~9s, range 6-14s.
    """

    def __init__(
        self,
        mean: float = 5.0,
        sigma: float = 1.5,
        min_delay: float = 3.0,
        max_delay: float = 10.0,
    ) -> None:
        self._mean = mean
        self._sigma = sigma
        self._min = min_delay
        self._max = max_delay

    def next_delay(self) -> float:
        delay = random.gauss(self._mean, self._sigma)
        return max(self._min, min(self._max, delay))

    async def wait(self) -> None:
        await asyncio.sleep(self.next_delay())