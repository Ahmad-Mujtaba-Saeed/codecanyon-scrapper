"""Pipeline tests: pagination, resume, dedupe, zero results, robots.

Runs entirely offline against a fake HTTP client, so it is safe to run in a
loop and never touches codecanyon.net.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccr.config import Config                                  # noqa: E402
from ccr.pipeline import Crawler                                # noqa: E402
from ccr.store import Store                                     # noqa: E402
from ccr.throttle import Throttle                               # noqa: E402

ROBOTS = b"User-agent: *\nDisallow: *?sort=*\nDisallow: /my/*\n"


def make_page(item_ids, total_results, has_next):
    """Minimal but structurally faithful search results page."""
    cards = []
    for item_id in item_ids:
        cards.append(
            f'<div data-item-id="{item_id}" data-price="29.00">'
            f'<h3><a href="https://codecanyon.net/item/thing-{item_id}/'
            f'{item_id}">Thing {item_id}</a></h3>'
            f'<a href="https://codecanyon.net/user/dev{item_id}">dev{item_id}</a>'
            f'<a href="/category/php-scripts/add-ons">Add-ons</a>'
            f'<div class="x-price_component__root">$29</div>'
            f'<div class="x-sales_component__root">{int(item_id) % 500} Sales</div>'
            f'<div class="x-stars_rating_component__starRating" '
            f'aria-label="Rated 4.5 out of 5, 7 reviews"></div>'
            f'<div class="x__lastUpdated">Last updated: 04 May 26</div>'
            f'</div>')
    head = '<link rel="next" href="/next">' if has_next else ""
    return (f"<html><head>{head}</head><body><h2>{total_results} results</h2>"
            f"{''.join(cards)}</body></html>").encode()


class FakeClient:
    """Serves canned pages and counts requests."""

    def __init__(self, pages):
        self.pages = pages          # {url: (status, body)}
        self.requests = []

    def get_raw(self, url, referer=None):
        if url.endswith("/robots.txt"):
            return 200, ROBOTS, {}
        return 404, b"", {}

    def get(self, url, referer=None, throttle=None, use_etag=True):
        self.requests.append(url)
        status, body = self.pages.get(url, (404, b""))
        return status, body, {}


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config({
            "base_url": "https://codecanyon.net",
            "sorts": ["relevance"],
            "respect_robots": True,
            "max_pages_per_keyword": 40,
            "items_per_page_expected": 30,
            "throttle": {
                "page_delay_min": 0, "page_delay_max": 0,
                "reading_pause_every_min": 99, "reading_pause_every_max": 99,
                "reading_pause_min": 0, "reading_pause_max": 0,
                "session_break_after_min": 99, "session_break_after_max": 99,
                "session_break_min": 0, "session_break_max": 0,
                "keyword_gap_min": 0, "keyword_gap_max": 0,
            },
            "http": {"timeout": 5, "max_retries": 1, "backoff_base": 0,
                     "backoff_max": 0, "consecutive_failure_abort": 3,
                     "user_agent": "test"},
            "parser": {"min_parse_success_ratio": 0.9},
            "paths": {"db": os.path.join(self.tmp, "db", "research.sqlite"),
                      "raw": os.path.join(self.tmp, "raw"),
                      "keywords": os.path.join(self.tmp, "keywords.csv")},
        }, path="<test>")
        self.store = Store(self.cfg.resolve("db"))
        self.slept = []

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def crawler(self, client, run_id):
        throttle = Throttle(self.cfg["throttle"], sleep=self.slept.append)
        return Crawler(self.cfg, self.store, client, throttle,
                       log=lambda m: None, run_id=run_id)


class TestPagination(PipelineTestCase):
    URL = "https://codecanyon.net/search/perfex%20integration"

    def build_client(self):
        return FakeClient({
            self.URL: (200, make_page(range(1, 31), 76, True)),
            self.URL + "?page=2": (200, make_page(range(31, 61), 76, True)),
            self.URL + "?page=3": (200, make_page(range(61, 77), 76, False)),
        })

    def test_follows_pages_until_no_next_link(self):
        client = self.build_client()
        self.crawler(client, "r1").crawl(["perfex integration"])

        self.assertEqual(len(client.requests), 3)
        summary = self.store.run_summary("r1")
        self.assertEqual(summary["pages"], 3)
        self.assertEqual(summary["occurrences"], 76)
        self.assertEqual(summary["unique_products"], 76)

    def test_positions_are_global_across_pages(self):
        self.crawler(self.build_client(), "r1").crawl(["perfex integration"])
        rows = self.store.conn.execute(
            "SELECT position FROM occurrences WHERE run_id='r1' "
            "ORDER BY position").fetchall()
        self.assertEqual(rows[0]["position"], 1)
        self.assertEqual(rows[-1]["position"], 76)

    def test_resume_skips_fetched_pages_and_keeps_keyword_stats(self):
        """Regression: a resumed run must not rewrite keywords as zero-result.

        Unique-product counts are derived from storage, not from the
        crawler's in-memory set, which is empty when every page is skipped.
        """
        self.crawler(self.build_client(), "r1").crawl(["perfex integration"])
        before = dict(self.store.conn.execute(
            "SELECT * FROM keyword_results WHERE run_id='r1'").fetchone())

        client = self.build_client()
        self.crawler(client, "r1").crawl(["perfex integration"])

        self.assertEqual(client.requests, [], "resume should not refetch")
        after = dict(self.store.conn.execute(
            "SELECT * FROM keyword_results WHERE run_id='r1'").fetchone())

        self.assertEqual(after["unique_products"], 76)
        self.assertEqual(after["total_results"], 76)
        self.assertEqual(after["zero_result"], 0)
        self.assertEqual(before["unique_products"], after["unique_products"])

    def test_resume_does_not_waste_time_pacing(self):
        self.crawler(self.build_client(), "r1").crawl(["perfex integration"])
        self.slept.clear()
        self.crawler(self.build_client(), "r1").crawl(
            ["perfex integration", "perfex mcp"])
        self.assertEqual(sum(self.slept), 0)


class TestDedupe(PipelineTestCase):
    def test_same_product_under_two_keywords_is_one_row_two_occurrences(self):
        client = FakeClient({
            "https://codecanyon.net/search/perfex%20api":
                (200, make_page([101, 102], 2, False)),
            "https://codecanyon.net/search/perfex%20mcp":
                (200, make_page([102, 103], 2, False)),
        })
        self.crawler(client, "r1").crawl(["perfex api", "perfex mcp"])

        summary = self.store.run_summary("r1")
        self.assertEqual(summary["occurrences"], 4)
        self.assertEqual(summary["unique_products"], 3)

        snapshots = self.store.conn.execute(
            "SELECT COUNT(*) FROM product_snapshots WHERE run_id='r1'"
        ).fetchone()[0]
        self.assertEqual(snapshots, 3, "one snapshot per product per run")

        shared = self.store.conn.execute(
            "SELECT COUNT(DISTINCT keyword) FROM occurrences "
            "WHERE run_id='r1' AND product_id='102'").fetchone()[0]
        self.assertEqual(shared, 2)


class TestZeroResults(PipelineTestCase):
    def test_zero_result_keyword_is_recorded_not_dropped(self):
        """Spec sections 21-22: an empty search is a competition signal."""
        client = FakeClient({
            "https://codecanyon.net/search/perfex%20quantum":
                (200, make_page([], 0, False)),
        })
        self.crawler(client, "r1").crawl(["perfex quantum"])

        row = self.store.conn.execute(
            "SELECT * FROM keyword_results WHERE run_id='r1'").fetchone()
        self.assertIsNotNone(row, "the keyword must still get a row")
        self.assertEqual(row["zero_result"], 1)
        self.assertEqual(row["total_results"], 0)
        self.assertEqual(row["unique_products"], 0)


class TestRobots(PipelineTestCase):
    def test_disallowed_url_is_never_requested(self):
        self.cfg._data["sorts"] = ["sales"]
        client = FakeClient({})
        self.crawler(client, "r1").crawl(["perfex api"], sorts=["sales"])

        self.assertEqual(client.requests, [])
        row = self.store.conn.execute(
            "SELECT error FROM crawl_pages WHERE run_id='r1'").fetchone()
        self.assertIn("robots.txt", row["error"])

    def test_ignoring_robots_is_possible_but_explicit(self):
        self.cfg._data["respect_robots"] = False
        url = "https://codecanyon.net/search/perfex%20api?sort=sales"
        client = FakeClient({url: (200, make_page([1], 1, False))})
        self.crawler(client, "r1").crawl(["perfex api"], sorts=["sales"])
        self.assertEqual(client.requests, [url])


class TestFailureHandling(PipelineTestCase):
    def test_http_error_stops_that_keyword_and_is_recorded(self):
        url = "https://codecanyon.net/search/perfex%20api"
        client = FakeClient({url: (503, b"")})
        self.crawler(client, "r1").crawl(["perfex api"])

        row = self.store.conn.execute(
            "SELECT * FROM crawl_pages WHERE run_id='r1'").fetchone()
        self.assertEqual(row["error"], "HTTP 503")
        self.assertEqual(self.store.run_summary("r1")["pages_failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
