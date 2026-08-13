"""Statistics, CSV export and report tests.

Built on a small hand-made dataset where every expected number can be
computed by hand, so a wrong statistic is caught by arithmetic rather than
by eyeballing a dashboard.
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccr import exporters, report, stats                        # noqa: E402
from ccr.config import Config                                    # noqa: E402
from ccr.models import Occurrence, Product                       # noqa: E402
from ccr.store import Store                                      # noqa: E402

# sales: 0, 10, 20, 100, 1000  -> total 1130, median 20, mean 226
FIXTURE = [
    # id,   title,                          author,  price, sales, updated
    ("1", "POS REST API Bridge",            "alice",  49.0,     0, "2026-08-01"),
    ("2", "POS WhatsApp Automation",        "alice",  29.0,    10, "2026-07-15"),
    ("3", "Ultimate POS MCP Connector",     "bob",    99.0,    20, "2020-01-01"),
    ("4", "POS Inventory Reports",          "bob",    19.0,   100, "2019-05-04"),
    ("5", "POS Accounting Automation",      "carol",  59.0,  1000, "2026-06-01"),
]

FEATURES = [
    {"name": "MCP", "pattern": r"\bmcp\b"},
    {"name": "API", "pattern": r"\bapi\b"},
    {"name": "Automation", "pattern": "automation"},
    {"name": "Blockchain", "pattern": "blockchain"},
]

ANALYSIS = {"stale_days": 365, "fresh_days": 90, "low_performer_sales": 5,
            "features": FEATURES}


class StatsTestCase(unittest.TestCase):
    RUN = "testrun"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config({
            "base_url": "https://codecanyon.net",
            "paths": {"db": os.path.join(self.tmp, "db", "t.sqlite"),
                      "csv": os.path.join(self.tmp, "csv"),
                      "reports": os.path.join(self.tmp, "reports")},
            "analysis": ANALYSIS,
        }, path="<test>")

        self.store = Store(self.cfg.resolve("db"))
        self.store.start_run(self.RUN, "POS", "{}")

        products, occurrences = [], []
        for i, (pid, title, author, price, sales, updated) in enumerate(FIXTURE):
            products.append(Product(
                product_id=pid, title=title,
                url=f"https://codecanyon.net/item/x/{pid}",
                author_name=author, author_url=f"https://codecanyon.net/user/{author}",
                category="php-scripts", subcategory="add-ons",
                price=price, sales=sales, rating=4.5, review_count=3,
                last_updated=updated))
            occurrences.append(Occurrence(pid, "ultimate pos", "relevance",
                                          1, i + 1))

        self.store.record_products(self.RUN, products)
        self.store.record_occurrences(self.RUN, occurrences)
        self.store.record_keyword_result(self.RUN, "ultimate pos", "relevance",
                                         total_results=5, pages_crawled=1,
                                         unique_products=5)
        # A keyword that found nothing at all.
        self.store.record_keyword_result(self.RUN, "ultimate pos quantum",
                                         "relevance", total_results=0,
                                         pages_crawled=1, unique_products=0)
        self.store.finish_run(self.RUN)
        self.conn = self.store.conn

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMarketOverview(StatsTestCase):
    def test_headline_numbers(self):
        o = stats.market_overview(self.conn, self.RUN)
        self.assertEqual(o["unique_products"], 5)
        self.assertEqual(o["total_sales"], 1130)
        self.assertEqual(o["median_sales"], 20)
        self.assertEqual(o["avg_sales"], 226.0)
        self.assertEqual(o["top_sales"], 1000)
        self.assertEqual(o["products_with_sales"], 4)
        self.assertEqual(o["products_without_sales"], 1)
        self.assertEqual(o["unique_authors"], 3)

    def test_revenue_is_sales_times_price(self):
        # 0*49 + 10*29 + 20*99 + 100*19 + 1000*59 = 63170
        o = stats.market_overview(self.conn, self.RUN)
        self.assertEqual(o["total_revenue_estimate"], 63170.0)

    def test_top_ten_share_is_capped_at_100(self):
        # Only 5 products, so the "top 10" is all of them.
        o = stats.market_overview(self.conn, self.RUN)
        self.assertEqual(o["top10_share_of_sales"], 100.0)


class TestDistribution(StatsTestCase):
    def test_buckets_partition_every_product_exactly_once(self):
        rows = stats.sales_distribution(self.conn, self.RUN)
        self.assertEqual(sum(r["products"] for r in rows), 5)
        by_bucket = {r["bucket"]: r["products"] for r in rows}
        self.assertEqual(by_bucket["no sales"], 1)     # 0
        self.assertEqual(by_bucket["10-49"], 2)        # 10, 20
        self.assertEqual(by_bucket["50-199"], 1)       # 100
        self.assertEqual(by_bucket["1000+"], 1)        # 1000
        self.assertEqual(by_bucket["1-9"], 0)
        self.assertEqual(by_bucket["200-999"], 0)


class TestKeywordSummary(StatsTestCase):
    def test_zero_result_keyword_is_present_with_zeroed_stats(self):
        rows = {r["keyword"]: r for r in
                stats.keyword_summary(self.conn, self.RUN)}
        self.assertIn("ultimate pos quantum", rows,
                      "a zero-result keyword must not vanish from the summary")
        empty = rows["ultimate pos quantum"]
        self.assertTrue(empty["zero_result"])
        self.assertEqual(empty["results"], 0)
        self.assertEqual(empty["top_sales"], 0)
        self.assertEqual(empty["median_sales"], 0)

    def test_populated_keyword_statistics(self):
        row = {r["keyword"]: r for r in
               stats.keyword_summary(self.conn, self.RUN)}["ultimate pos"]
        self.assertEqual(row["unique_products"], 5)
        self.assertEqual(row["total_sales"], 1130)
        self.assertEqual(row["top_sales"], 1000)
        self.assertEqual(row["median_sales"], 20)
        self.assertFalse(row["zero_result"])


class TestFeatureAnalysis(StatsTestCase):
    def test_counts_and_sales_per_feature(self):
        rows = {r["feature"]: r for r in
                stats.feature_analysis(self.conn, self.RUN, FEATURES)}

        self.assertEqual(rows["MCP"]["products"], 1)
        self.assertEqual(rows["MCP"]["total_sales"], 20)
        self.assertEqual(rows["Automation"]["products"], 2)      # ids 2 and 5
        self.assertEqual(rows["Automation"]["total_sales"], 1010)

    def test_word_boundaries_prevent_false_matches(self):
        # "API" must not match inside "Rapid" and MCP must not match "MCPX".
        rows = {r["feature"]: r for r in
                stats.feature_analysis(self.conn, self.RUN, FEATURES)}
        self.assertEqual(rows["API"]["products"], 1)             # id 1 only

    def test_feature_with_no_products_is_kept_not_dropped(self):
        rows = {r["feature"]: r for r in
                stats.feature_analysis(self.conn, self.RUN, FEATURES)}
        self.assertIn("Blockchain", rows)
        self.assertEqual(rows["Blockchain"]["products"], 0)
        self.assertEqual(rows["Blockchain"]["total_sales"], 0)


class TestAgeAnalysis(StatsTestCase):
    def test_stale_and_fresh_classification(self):
        import datetime
        today = datetime.date(2026, 8, 12)
        age = stats.age_analysis(self.conn, self.RUN, ANALYSIS, today=today)

        # ids 3 and 4 last updated in 2020 and 2019
        self.assertEqual(age["unmaintained"], 2)
        # of those, only id 4 (100 sales) is above the median of 20
        self.assertEqual(age["old_still_selling"], 1)
        # ids 2 and 5 updated within 90 days and have sales; id 1 has none
        self.assertEqual(age["new_getting_sales"], 2)
        # sales <= 5: only id 1 (zero sales)
        self.assertEqual(age["low_performers"], 1)


class TestAuthors(StatsTestCase):
    def test_shares_sum_to_100(self):
        rows = stats.author_concentration(self.conn, self.RUN)
        self.assertEqual(rows[0]["author"], "carol")     # 1000 of 1130
        self.assertEqual(rows[0]["share"], 88.5)
        self.assertAlmostEqual(sum(r["share"] for r in rows), 100.0, delta=0.2)


class TestExports(StatsTestCase):
    def read(self, name, run_scoped=True):
        base = self.cfg.resolve("csv")
        path = (os.path.join(base, self.RUN, name) if run_scoped
                else os.path.join(base, name))
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_all_five_files_are_written(self):
        paths = exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        self.assertEqual(len(paths), 5)
        for path in paths:
            self.assertTrue(os.path.exists(path), path)

    def test_products_csv_has_one_row_per_product(self):
        exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        rows = self.read("products.csv")
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({r["product_id"] for r in rows}), 5)
        self.assertEqual(sum(int(r["sales"]) for r in rows), 1130)

    def test_occurrences_csv_keeps_position_data(self):
        exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        rows = self.read("search_occurrences.csv")
        self.assertEqual(len(rows), 5)
        self.assertEqual([r["position"] for r in rows],
                         ["1", "2", "3", "4", "5"])

    def test_keyword_summary_csv_includes_zero_result_row(self):
        exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        rows = {r["keyword"]: r for r in self.read("keyword_summary.csv")}
        self.assertEqual(rows["ultimate pos quantum"]["zero_result"], "yes")
        self.assertEqual(rows["ultimate pos"]["zero_result"], "no")

    def test_research_runs_csv_is_cross_run_and_at_the_top_level(self):
        exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        rows = self.read("research_runs.csv", run_scoped=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], self.RUN)
        self.assertEqual(rows[0]["unique_products"], "5")

    def test_runs_are_exported_to_separate_directories(self):
        """Later runs must not overwrite earlier ones -- that history is what
        makes growth analysis possible."""
        exporters.export_all(self.conn, self.RUN, self.cfg.resolve("csv"))
        self.store.start_run("run2", "POS", "{}")
        self.store.record_keyword_result("run2", "k", "relevance", 0, 1, 0)
        exporters.export_all(self.conn, "run2", self.cfg.resolve("csv"))

        base = self.cfg.resolve("csv")
        self.assertTrue(os.path.isdir(os.path.join(base, self.RUN)))
        self.assertTrue(os.path.isdir(os.path.join(base, "run2")))
        self.assertEqual(len(self.read("products.csv")), 5)


class TestReport(StatsTestCase):
    def test_report_renders_with_real_numbers(self):
        path = report.write_report(self.conn, self.RUN, self.cfg)
        with open(path, encoding="utf-8") as f:
            html = f.read()

        self.assertIn("<!doctype html>", html)
        self.assertIn("1,130", html)                      # total sales
        self.assertIn("Ultimate POS MCP Connector", html)
        self.assertIn("zero results", html)               # the empty keyword
        self.assertIn("--series-1", html)                 # stylesheet inlined
        self.assertNotIn("<link", html)                   # nothing external

    def test_titles_are_html_escaped(self):
        self.conn.execute(
            "UPDATE products SET title='<script>alert(1)</script>' "
            "WHERE product_id='1'")
        self.conn.commit()
        with open(report.write_report(self.conn, self.RUN, self.cfg),
                  encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_bar_widths_are_proportional_and_bounded(self):
        rows = [("a", 50, "50"), ("b", 100, "100"), ("c", 0, "0")]
        markup = report.bars(rows)
        self.assertIn("width:50.0%", markup)
        self.assertIn("width:100.0%", markup)
        self.assertIn("width:0.0%", markup)

    def test_empty_chart_does_not_crash(self):
        self.assertIn("No data", report.bars([]))
        self.assertIn("No data", report.table(["a"], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
