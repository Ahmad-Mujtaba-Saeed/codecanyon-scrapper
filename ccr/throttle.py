"""Human-like pacing.

The point is not just to be slow, it is to be *irregular*. A request every
exactly 5.0 seconds for 90 minutes is a stronger bot signal than a request
every 2 seconds, because nothing human produces a flat interval.

Four layers, all configurable:

  page delay      4-9s between consecutive pages, log-normal shaped so most
                  gaps sit near the low end with an occasional long one
  reading pause   30-90s every 5-8 pages, as if someone stopped to read
  session break   3-8 minutes every 45-60 requests
  keyword gap     20-60s when moving to a new search

Requests are strictly sequential; there is no concurrency anywhere.
"""

import random
import time


def _lognormal_between(low, high, sigma=0.45):
    """Draw from a log-normal skewed toward the low end, clamped to range."""
    if high <= low:
        return low
    span = high - low
    # median of exp(N(0, sigma)) is 1.0; divide by 3 to place the median at
    # roughly a third of the range rather than the middle
    sample = random.lognormvariate(0.0, sigma) / 3.0
    return low + min(span, span * sample)


class Throttle:
    def __init__(self, cfg, sleep=time.sleep, log=None):
        self.cfg = cfg
        self._sleep = sleep
        self._log = log or (lambda msg: None)

        self.requests_made = 0
        self.pages_since_pause = 0
        self.requests_since_break = 0

        self._next_pause_at = random.randint(
            cfg["reading_pause_every_min"], cfg["reading_pause_every_max"])
        self._next_break_at = random.randint(
            cfg["session_break_after_min"], cfg["session_break_after_max"])

        # Total time spent sleeping, useful for run reporting.
        self.slept_total = 0.0

    # ------------------------------------------------------------ internals

    def _pause(self, seconds, label):
        seconds = round(seconds, 1)
        self.slept_total += seconds
        self._log(f"  ~ {label} {seconds}s")
        self._sleep(seconds)

    # -------------------------------------------------------------- public

    def before_page(self, is_first_request=False):
        """Called before every page fetch."""
        if is_first_request:
            return

        cfg = self.cfg

        if self.requests_since_break >= self._next_break_at:
            self._pause(
                random.uniform(cfg["session_break_min"],
                               cfg["session_break_max"]),
                "session break")
            self.requests_since_break = 0
            self._next_break_at = random.randint(
                cfg["session_break_after_min"], cfg["session_break_after_max"])
            self.pages_since_pause = 0
            return

        if self.pages_since_pause >= self._next_pause_at:
            self._pause(
                random.uniform(cfg["reading_pause_min"],
                               cfg["reading_pause_max"]),
                "reading pause")
            self.pages_since_pause = 0
            self._next_pause_at = random.randint(
                cfg["reading_pause_every_min"], cfg["reading_pause_every_max"])
            return

        self._pause(
            _lognormal_between(cfg["page_delay_min"], cfg["page_delay_max"]),
            "page delay")

    def after_page(self):
        self.requests_made += 1
        self.pages_since_pause += 1
        self.requests_since_break += 1

    def between_keywords(self):
        self._pause(
            random.uniform(self.cfg["keyword_gap_min"],
                           self.cfg["keyword_gap_max"]),
            "keyword gap")

    def backoff(self, attempt, base, maximum):
        """Exponential backoff with jitter, for retries."""
        delay = min(maximum, base * (2 ** attempt))
        delay *= random.uniform(0.7, 1.3)
        self._pause(delay, f"backoff (attempt {attempt + 1})")
