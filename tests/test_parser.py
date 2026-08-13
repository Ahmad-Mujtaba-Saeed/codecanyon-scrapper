"""Parser tests against a real captured page.

The fixture is the actual response for

    https://codecanyon.net/search/perfex%20integration?page=2

captured 2026-08-12. The assertions below pin real values from that page,
so if Envato rotates its class names these tests fail immediately and
loudly rather than the scraper silently collecting nulls.

Run: python -m unittest discover -s tests -v
"""

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccr import parser                                        # noqa: E402
from ccr.urls import search_url, keyword_slug                  # noqa: E402
from ccr.robots import RobotsRules                             # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "perfex-integration-page2.html.gz")
PAGE_URL = "https://codecanyon.net/search/perfex%20integration?page=2"


def load_fixture():
    with gzip.open(FIXTURE, "rb") as f:
        return f.read()


class TestNormalizers(unittest.TestCase):
    """Spec section 14: raw values normalize to one canonical form."""

    def test_price(self):
        for raw in ("$29", "29 USD", "USD 29", "29.00", "29"):
            self.assertEqual(parser.parse_price(raw), 29.0, raw)
        self.assertIsNone(parser.parse_price(None))
        self.assertIsNone(parser.parse_price("free"))

    def test_sales(self):
        self.assertEqual(parser.parse_sales("3K Sales"), 3000)
        self.assertEqual(parser.parse_sales("3,000 Sales"), 3000)
        self.assertEqual(parser.parse_sales("3000"), 3000)
        self.assertEqual(parser.parse_sales("145 Sales"), 145)
        self.assertEqual(parser.parse_sales("1.5K Sales"), 1500)
        self.assertEqual(parser.parse_sales("2M Sales"), 2_000_000)

    def test_rating(self):
        self.assertEqual(
            parser.parse_rating("Rated 5.0 out of 5, 3 reviews"), (5.0, 3))
        self.assertEqual(
            parser.parse_rating("Rated 4.5 out of 5, 1 review"), (4.5, 1))
        self.assertEqual(parser.parse_rating(None), (None, None))

    def test_updated(self):
        self.assertEqual(parser.parse_updated("Last updated: 04 May 26"),
                         "2026-05-04")
        self.assertEqual(parser.parse_updated("31 December 24"), "2024-12-31")
        self.assertIsNone(parser.parse_updated("recently"))

    def test_item_id_from_url(self):
        self.assertEqual(parser.parse_item_id(
            "https://codecanyon.net/item/allinone-support-module-for-perfex/"
            "25269490"), "25269490")


class TestUrls(unittest.TestCase):
    """Spec section 5: the scraper builds its own URLs from keywords."""

    BASE = "https://codecanyon.net"

    def test_encoding_and_pagination(self):
        self.assertEqual(
            search_url(self.BASE, "ultimate pos integration"),
            "https://codecanyon.net/search/ultimate%20pos%20integration")
        self.assertEqual(
            search_url(self.BASE, "ultimate pos integration", page=2),
            "https://codecanyon.net/search/ultimate%20pos%20integration?page=2")
        self.assertEqual(
            search_url(self.BASE, "ultimate pos", sort="sales", page=3),
            "https://codecanyon.net/search/ultimate%20pos?sort=sales&page=3")

    def test_relevance_carries_no_sort_param(self):
        self.assertNotIn("sort=", search_url(self.BASE, "pos", page=4))

    def test_slug(self):
        self.assertEqual(keyword_slug("Ultimate POS  integration!"),
                         "ultimate-pos-integration")

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            search_url(self.BASE, "pos", sort="nonsense")
        with self.assertRaises(ValueError):
            search_url(self.BASE, "pos", page=0)


class TestRobots(unittest.TestCase):
    """The real codecanyon.net rules, as fetched 2026-08-12."""

    ROBOTS = (
        "User-agent: *\n"
        "\n"
        "Disallow: *?platform*\n"
        "Disallow: */full_screen_preview/\n"
        "Disallow: *?sales*\n"
        "Disallow: /cart/\n"
        "Disallow: *?sort=*\n"
        "Disallow: /my/*\n"
        "Disallow: /cart$\n"
        "\n"
        "User-agent: CCBot\n"
        "Disallow: /\n"
    )

    def setUp(self):
        self.rules = RobotsRules.parse(self.ROBOTS)

    def test_paginated_search_is_allowed(self):
        self.assertTrue(self.rules.allowed(PAGE_URL))
        self.assertTrue(self.rules.allowed(
            "https://codecanyon.net/search/ultimate%20pos"))

    def test_sorted_search_is_disallowed(self):
        self.assertFalse(self.rules.allowed(
            "https://codecanyon.net/search/ultimate%20pos?sort=sales"))
        self.assertEqual(
            self.rules.reason(
                "https://codecanyon.net/search/ultimate%20pos?sort=date"),
            "^.*\\?sort=.*")

    def test_other_wildcard_rules(self):
        self.assertFalse(self.rules.allowed(
            "https://codecanyon.net/item/x/full_screen_preview/123"))
        self.assertFalse(self.rules.allowed("https://codecanyon.net/my/favorites"))

    def test_dollar_anchor(self):
        self.assertFalse(self.rules.allowed("https://codecanyon.net/cart"))

    def test_ccbot_group_does_not_leak_into_star_group(self):
        # If the CCBot "Disallow: /" leaked in, everything would be blocked.
        self.assertTrue(self.rules.allowed("https://codecanyon.net/anything"))


class TestSearchPage(unittest.TestCase):
    """Full-page parse pinned to known values from the captured page."""

    @classmethod
    def setUpClass(cls):
        cls.result = parser.parse_search_page(
            load_fixture(), keyword="perfex integration", sort="relevance",
            page=2, url=PAGE_URL)

    def test_page_level_facts(self):
        self.assertEqual(self.result.cards_seen, 30)
        self.assertEqual(self.result.cards_parsed, 30)
        self.assertEqual(self.result.total_results, 150)
        self.assertTrue(self.result.has_next)
        self.assertEqual(len(self.result.products), 30)

    def test_positions_are_global_not_page_local(self):
        positions = [o.position for o in self.result.occurrences]
        self.assertEqual(positions[0], 31)     # page 2, first card
        self.assertEqual(positions[-1], 60)
        self.assertEqual(len(set(positions)), 30)

    def test_first_product_fields(self):
        p = self.result.products[0]
        self.assertEqual(p.product_id, "25269490")
        self.assertEqual(
            p.title,
            "Chat Support Module - WhatsApp, Messenger & Viber Integration "
            "for Perfex CRM")
        self.assertEqual(
            p.url,
            "https://codecanyon.net/item/allinone-support-module-for-perfex/"
            "25269490")
        self.assertEqual(p.author_name, "themesic")
        self.assertEqual(p.author_url, "https://codecanyon.net/user/themesic")
        self.assertEqual(p.category, "php-scripts")
        self.assertEqual(p.subcategory, "add-ons")
        self.assertEqual(p.price, 49.0)
        self.assertEqual(p.sales, 145)
        self.assertEqual(p.rating, 5.0)
        self.assertEqual(p.review_count, 3)
        self.assertEqual(p.software_version, "PHP 7.x")
        self.assertEqual(p.framework, "CodeIgniter")
        self.assertEqual(p.last_updated, "2026-05-04")

    def test_title_strips_search_highlight_markup(self):
        # The search engine wraps matched words in <mark> tags.
        for p in self.result.products:
            self.assertNotIn("<mark", p.title)
            self.assertNotIn("</mark>", p.title)

    def test_every_product_has_identity(self):
        for p in self.result.products:
            self.assertTrue(p.product_id and p.product_id.isdigit())
            self.assertTrue(p.url.startswith("https://codecanyon.net/item/"))
            self.assertTrue(p.title)

    def test_product_ids_are_unique_within_page(self):
        ids = [p.product_id for p in self.result.products]
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_sales_element_means_zero_not_null(self):
        # One card on this page has no sales element at all.
        zero = [p for p in self.result.products if p.sales == 0]
        self.assertEqual(len(zero), 1)
        self.assertIsNone(zero[0].sales_raw)
        self.assertTrue(all(p.sales is not None for p in self.result.products))

    def test_prices_come_from_data_attribute(self):
        # data-price is exact; the visible "$49" text is rounded.
        for p in self.result.products:
            self.assertIsNotNone(p.price)
            self.assertGreaterEqual(p.price, 0)

    def test_health_guard_trips_when_cards_vanish_entirely(self):
        # A rotated attribute name makes every card invisible to the selector.
        # The page still says "150 results", so this must not look like an
        # empty result set.
        broken = load_fixture().replace(b"data-item-id=", b"data-xxx-id=")
        with self.assertRaises(parser.ParserHealthError):
            parser.parse_search_page(broken, "perfex integration", "relevance",
                                     2, PAGE_URL)

    def test_health_guard_trips_on_partial_parse_failure(self):
        good = ('<div data-item-id="111" data-price="49.00">'
                '<h3><a href="https://codecanyon.net/item/x/111">X</a></h3>'
                '</div>')
        # Cards that are found but yield no identity: id present, link gone.
        bad = '<div data-item-id="222" data-price="10.00"><h3>Y</h3></div>' * 9
        html = (f"<html><head></head><body><h2>10 results</h2>"
                f"{good}{bad}</body></html>").encode()
        with self.assertRaises(parser.ParserHealthError):
            parser.parse_search_page(html, "k", "relevance", 1, PAGE_URL)


class TestEmptyPage(unittest.TestCase):
    """Spec sections 21-22: zero results is data, not an error."""

    HTML = ("<html><head></head><body>"
            "<h2>0 results</h2><p>Your search didn't match any items.</p>"
            "</body></html>")

    def test_zero_results_parses_cleanly(self):
        r = parser.parse_search_page(
            self.HTML.encode(), "ultimate pos mcp", "relevance", 1,
            "https://codecanyon.net/search/ultimate%20pos%20mcp")
        self.assertEqual(r.total_results, 0)
        self.assertEqual(r.products, [])
        self.assertFalse(r.has_next)
        self.assertEqual(r.parse_ratio, 1.0)   # no cards seen is not a failure


if __name__ == "__main__":
    unittest.main(verbosity=2)
