"""Phase 7 (AI keywords and analysis) and Phase 8 (cross-run diffing).

The OpenAI calls run against an injected transport, so the request shape and
the response handling are both tested without a key and without network
access.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccr import ai, analysis, diff                              # noqa: E402
from ccr import keywords as keyword_file                        # noqa: E402
from ccr.config import Config                                    # noqa: E402
from ccr.log import Logger                                       # noqa: E402
from ccr.models import Occurrence, Product                       # noqa: E402
from ccr.store import Store                                      # noqa: E402

AI_CFG = {"model": "gpt-4o-mini", "api_base": "https://api.openai.com/v1",
          "api_key_env": "CCR_TEST_KEY", "temperature": 0.4,
          "max_output_tokens": 1000, "timeout": 5, "keyword_count": 5,
          "bundle_top_products": 10}


def fake_transport(reply, capture=None):
    def transport(path, payload):
        if capture is not None:
            capture.append((path, payload))
        return {"choices": [{"message": {"content": reply}}]}
    return transport


# ------------------------------------------------------------------- client

class TestOpenAIClient(unittest.TestCase):
    def test_request_shape(self):
        seen = []
        client = ai.OpenAIClient(AI_CFG, transport=fake_transport("hi", seen))
        out = client.chat("be brief", "hello", json_mode=True)

        self.assertEqual(out, "hi")
        path, payload = seen[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["content"], "hello")
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_missing_key_raises_unavailable_not_a_crash(self):
        os.environ.pop("CCR_TEST_KEY", None)
        self.assertFalse(ai.available(AI_CFG))
        with self.assertRaises(ai.AIUnavailable):
            ai.OpenAIClient(AI_CFG).chat("a", "b")

    def test_available_reads_the_configured_env_var(self):
        os.environ["CCR_TEST_KEY"] = "sk-test"
        try:
            self.assertTrue(ai.available(AI_CFG))
        finally:
            os.environ.pop("CCR_TEST_KEY")

    def test_malformed_response_is_reported_clearly(self):
        client = ai.OpenAIClient(AI_CFG, transport=lambda p, b: {"nope": 1})
        with self.assertRaises(ai.AIError):
            client.chat("a", "b")


class TestKeywordParsing(unittest.TestCase):
    def parse(self, raw):
        return ai.parse_keyword_response(raw, "Ultimate POS")

    def test_plain_json(self):
        rows = self.parse(json.dumps({"keywords": [
            {"keyword": "ultimate pos", "band": "broad", "priority": "high",
             "rationale": "sizes the market"},
            {"keyword": "ultimate pos mcp", "band": "speculative",
             "priority": "low", "rationale": "tests an empty niche"}]}))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["keyword"], "ultimate pos")
        self.assertEqual(rows[0]["parent_topic"], "Ultimate POS")
        self.assertEqual(rows[0]["source"], "ai")

    def test_generated_keywords_are_never_pre_approved(self):
        """The approval gate is the whole point: a model must not be able to
        cause a crawl on its own."""
        rows = self.parse('{"keywords": [{"keyword": "ultimate pos api"}]}')
        self.assertFalse(rows[0]["approved"])

    def test_tolerates_code_fences(self):
        rows = self.parse('```json\n{"keywords": ["ultimate pos api"]}\n```')
        self.assertEqual(rows[0]["keyword"], "ultimate pos api")

    def test_tolerates_prose_around_the_json(self):
        rows = self.parse('Sure! Here you go:\n'
                          '{"keywords": ["ultimate pos"]}\nHope that helps.')
        self.assertEqual(rows[0]["keyword"], "ultimate pos")

    def test_lowercases_and_deduplicates(self):
        rows = self.parse('{"keywords": ["Ultimate POS", "ultimate pos"]}')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword"], "ultimate pos")

    def test_rejects_search_syntax_and_overlong_entries(self):
        rows = self.parse(json.dumps({"keywords": [
            "ultimate pos", '"quoted phrase"', "site:codecanyon.net pos",
            "a" * 200]}))
        self.assertEqual([r["keyword"] for r in rows], ["ultimate pos"])

    def test_unparseable_response_raises(self):
        with self.assertRaises(ai.AIError):
            self.parse("I cannot help with that.")

    def test_empty_keyword_list_raises(self):
        with self.assertRaises(ai.AIError):
            self.parse('{"keywords": []}')

    def test_generate_keywords_end_to_end(self):
        client = ai.OpenAIClient(AI_CFG, transport=fake_transport(
            '{"keywords": [{"keyword": "perfex mcp", "band": "speculative"}]}'))
        rows = ai.generate_keywords(client, "Perfex CRM", ["perfex api"], 5)
        self.assertEqual(rows[0]["keyword"], "perfex mcp")


# ----------------------------------------------------------- approval gate

class TestKeywordFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "keywords.csv")
        keyword_file.save(self.path, [
            {"keyword": "perfex api", "parent_topic": "Perfex",
             "source": "manual", "approved": True, "priority": "high"},
            {"keyword": "perfex mcp", "parent_topic": "Perfex",
             "source": "ai", "approved": False, "priority": "low"},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_hides_unapproved_by_default(self):
        self.assertEqual([r["keyword"] for r in keyword_file.load(self.path)],
                         ["perfex api"])
        self.assertEqual(len(keyword_file.load(self.path,
                                               include_unapproved=True)), 2)

    def test_merge_adds_only_new_keywords(self):
        added, skipped = keyword_file.merge(self.path, [
            {"keyword": "perfex api", "source": "ai", "approved": False},
            {"keyword": "perfex webhook", "source": "ai", "approved": False},
        ])
        self.assertEqual([r["keyword"] for r in added], ["perfex webhook"])
        self.assertEqual(len(skipped), 1)

    def test_merge_preserves_existing_approval_state(self):
        """Regenerating must not re-approve something a human rejected, nor
        unapprove something they approved."""
        keyword_file.merge(self.path, [
            {"keyword": "perfex api", "source": "ai", "approved": False},
            {"keyword": "perfex mcp", "source": "ai", "approved": True},
        ])
        rows = {r["keyword"]: r for r in
                keyword_file.load(self.path, include_unapproved=True)}
        self.assertTrue(rows["perfex api"]["approved"])
        self.assertFalse(rows["perfex mcp"]["approved"])

    def test_approve_specific_and_all(self):
        keyword_file.set_approval(self.path, True, ["perfex mcp"])
        rows = {r["keyword"]: r for r in
                keyword_file.load(self.path, include_unapproved=True)}
        self.assertTrue(rows["perfex mcp"]["approved"])

        keyword_file.set_approval(self.path, False)
        self.assertEqual(keyword_file.load(self.path), [])


# -------------------------------------------------------------- data fixture

class DataTestCase(unittest.TestCase):
    """Two runs of the same market, three months apart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config({
            "paths": {"db": os.path.join(self.tmp, "db", "t.sqlite"),
                      "analysis": os.path.join(self.tmp, "analysis"),
                      "csv": os.path.join(self.tmp, "csv")},
            "ai": AI_CFG,
            "analysis": {"stale_days": 365, "fresh_days": 90,
                         "low_performer_sales": 5,
                         "features": [{"name": "MCP", "pattern": r"\bmcp\b"},
                                      {"name": "API", "pattern": r"\bapi\b"}]},
        }, path="<test>")
        self.store = Store(self.cfg.resolve("db"))
        self.conn = self.store.conn

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_run(self, run_id, rows, keywords=("ultimate pos",), started=None):
        self.store.start_run(run_id, "Ultimate POS", "{}")
        if started:
            self.conn.execute(
                "UPDATE research_runs SET started_at=? WHERE run_id=?",
                (started, run_id))

        products, occurrences = [], []
        for position, (pid, title, price, sales) in enumerate(rows, start=1):
            products.append(Product(
                product_id=pid, title=title,
                url=f"https://codecanyon.net/item/x/{pid}",
                author_name="alice", price=price, sales=sales,
                rating=4.5, review_count=2, last_updated="2026-06-01"))
            occurrences.append(Occurrence(pid, keywords[0], "relevance", 1,
                                          position))

        self.store.record_products(run_id, products)
        self.store.record_occurrences(run_id, occurrences)
        for keyword in keywords:
            self.store.record_keyword_result(
                run_id, keyword, "relevance", total_results=len(rows),
                pages_crawled=1, unique_products=len(rows))
        self.store.finish_run(run_id)


# --------------------------------------------------------------------- diff

class TestDiff(DataTestCase):
    def setUp(self):
        super().setUp()
        # id 1 grows, id 2 unchanged, id 3 disappears, id 4 is new.
        self.add_run("may", [
            ("1", "POS MCP Bridge", 49.0, 20),
            ("2", "POS API Kit", 29.0, 100),
            ("3", "POS Legacy Sync", 19.0, 5),
        ], started="2026-05-01T00:00:00+00:00")
        self.add_run("august", [
            ("1", "POS MCP Bridge", 59.0, 143),
            ("2", "POS API Kit", 29.0, 100),
            ("4", "POS AI Copilot", 79.0, 12),
        ], started="2026-08-01T00:00:00+00:00")
        self.result = diff.compare_runs(self.conn, "may", "august")

    def test_totals(self):
        totals = self.result["totals"]
        self.assertEqual(totals["products_before"], 3)
        self.assertEqual(totals["products_after"], 3)
        self.assertEqual(totals["appeared"], 1)
        self.assertEqual(totals["disappeared"], 1)
        # 20+100+5 = 125 -> 143+100+12 = 255
        self.assertEqual(totals["sales_before"], 125)
        self.assertEqual(totals["sales_after"], 255)
        self.assertEqual(totals["sales_delta"], 130)

    def test_growth_is_measured_per_product(self):
        changed = {c["product_id"]: c for c in self.result["changed"]}
        self.assertEqual(changed["1"]["sales_before"], 20)
        self.assertEqual(changed["1"]["sales_after"], 143)
        self.assertEqual(changed["1"]["sales_delta"], 123)
        self.assertEqual(changed["1"]["price_delta"], 10.0)

    def test_unchanged_products_are_not_listed_as_changed(self):
        self.assertNotIn("2", {c["product_id"] for c in self.result["changed"]})

    def test_appeared_and_disappeared(self):
        self.assertEqual([p["product_id"] for p in self.result["appeared"]],
                         ["4"])
        self.assertEqual([p["product_id"] for p in self.result["disappeared"]],
                         ["3"])

    def test_scope_matches_when_keywords_are_identical(self):
        self.assertTrue(self.result["scope"]["matches"])
        self.assertEqual(self.result["scope"]["only_in_a"], [])
        self.assertEqual(self.result["scope"]["only_in_b"], [])

    def test_missing_run_is_rejected(self):
        with self.assertRaises(ValueError):
            diff.compare_runs(self.conn, "may", "nonexistent")

    def test_appeared_product_updated_before_the_earlier_run_is_flagged(self):
        """Observed for real: paginating a live result set skipped two items,
        which then looked like new arrivals in the next run. A product whose
        last-updated date predates the earlier run cannot be a new arrival."""
        # The per-run snapshot is the source of truth for what a run observed,
        # so that is what the check reads.
        self.conn.execute(
            "UPDATE product_snapshots SET last_updated='2026-01-15' "
            "WHERE product_id='4' AND run_id='august'")
        self.conn.commit()
        result = diff.compare_runs(self.conn, "may", "august")

        appeared = result["appeared"][0]
        self.assertTrue(appeared["existed_before"])
        self.assertEqual(result["totals"]["appeared_existing"], 1)

    def test_genuinely_new_product_is_not_flagged(self):
        self.conn.execute(
            "UPDATE product_snapshots SET last_updated='2026-07-20' "
            "WHERE product_id='4' AND run_id='august'")
        self.conn.commit()
        result = diff.compare_runs(self.conn, "may", "august")
        self.assertFalse(result["appeared"][0]["existed_before"])
        self.assertEqual(result["totals"]["appeared_existing"], 0)

    def test_incomplete_pagination_coverage_is_reported(self):
        # The site said 150 results; the run captured 3 distinct products.
        self.conn.execute(
            "UPDATE keyword_results SET total_results=150 "
            "WHERE run_id='may' AND keyword='ultimate pos'")
        self.conn.commit()
        result = diff.compare_runs(self.conn, "may", "august")

        self.assertTrue(result["coverage"])
        entry = result["coverage"][0]
        self.assertEqual(entry["missed"], 147)
        self.assertEqual(entry["keyword"], "ultimate pos")

    def test_complete_coverage_reports_nothing(self):
        result = diff.compare_runs(self.conn, "may", "august")
        self.assertEqual(result["coverage"], [])


class TestDiffScopeMismatch(DataTestCase):
    """The failure this module exists to prevent.

    If run B searched extra keywords, products found only under those
    keywords must NOT be reported as market newcomers.
    """

    def setUp(self):
        super().setUp()
        self.add_run("narrow", [("1", "POS MCP Bridge", 49.0, 20)],
                     keywords=("ultimate pos",))

        # A second run over two keywords, the extra one finding a product
        # that was always there but was never searched for before.
        self.store.start_run("wide", "Ultimate POS", "{}")
        self.store.record_products("wide", [
            Product(product_id="1", title="POS MCP Bridge",
                    url="https://codecanyon.net/item/x/1", price=49.0, sales=20),
            Product(product_id="9", title="POS Inventory",
                    url="https://codecanyon.net/item/x/9", price=39.0, sales=800),
        ])
        self.store.record_occurrences("wide", [
            Occurrence("1", "ultimate pos", "relevance", 1, 1),
            Occurrence("9", "ultimate pos inventory", "relevance", 1, 1),
        ])
        for keyword in ("ultimate pos", "ultimate pos inventory"):
            self.store.record_keyword_result("wide", keyword, "relevance",
                                             1, 1, 1)
        self.store.finish_run("wide")

        self.result = diff.compare_runs(self.conn, "narrow", "wide")

    def test_scope_difference_is_reported(self):
        scope = self.result["scope"]
        self.assertFalse(scope["matches"])
        self.assertEqual(scope["shared"], ["ultimate pos"])
        self.assertEqual(scope["only_in_b"], ["ultimate pos inventory"])

    def test_products_from_new_keywords_are_not_counted_as_new(self):
        self.assertEqual(self.result["totals"]["appeared"], 0)
        self.assertEqual([p["product_id"] for p in self.result["appeared"]], [])

    def test_shared_scope_totals_exclude_the_extra_keyword(self):
        # 800-sale product came from the unshared keyword and must not
        # inflate the comparison.
        self.assertEqual(self.result["totals"]["sales_before"], 20)
        self.assertEqual(self.result["totals"]["sales_after"], 20)

    def test_no_shared_keywords_yields_an_empty_but_valid_comparison(self):
        self.add_run("other", [("7", "Something else", 10.0, 1)],
                     keywords=("totally different",))
        result = diff.compare_runs(self.conn, "narrow", "other")
        self.assertEqual(result["scope"]["shared"], [])
        self.assertEqual(result["totals"]["products_before"], 0)
        self.assertEqual(result["totals"]["appeared"], 0)

    def test_text_output_warns_about_scope(self):
        import io
        buffer = io.StringIO()
        diff.format_text(self.result, Logger(stream=buffer))
        text = buffer.getvalue()
        self.assertIn("WARNING", text)
        self.assertIn("ultimate pos inventory", text)
        self.assertIn("scope-limited", text)


# ----------------------------------------------------------------- analysis

class TestAnalysisBundle(DataTestCase):
    def setUp(self):
        super().setUp()
        self.add_run("run1", [
            ("1", "POS MCP Bridge", 49.0, 20),
            ("2", "POS API Kit", 29.0, 100),
        ])
        self.store.record_keyword_result("run1", "ultimate pos quantum",
                                         "relevance", 0, 1, 0)

    def test_bundle_contains_the_numbers_and_the_caveats(self):
        text, topic = analysis.build_bundle(self.conn, "run1", self.cfg)
        self.assertEqual(topic, "Ultimate POS")
        self.assertIn("POS MCP Bridge", text)
        self.assertIn("120", text)                       # total sales
        # The caveats are the point: without them the model misreads the data.
        self.assertIn("lifetime totals", text)
        self.assertIn("robots.txt", text)
        self.assertIn("what vendors advertise", text)

    def test_zero_result_keyword_reaches_the_model(self):
        text, _ = analysis.build_bundle(self.conn, "run1", self.cfg)
        self.assertIn("ultimate pos quantum", text)

    def test_writes_dataset_and_paste_ready_prompt(self):
        bundle = analysis.write_bundle(self.conn, "run1", self.cfg)
        self.assertTrue(os.path.exists(bundle["dataset"]))
        self.assertTrue(os.path.exists(bundle["prompt"]))

        with open(bundle["prompt"], encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("## Opportunities", prompt)
        self.assertIn("## Recommendation", prompt)
        self.assertIn("POS MCP Bridge", prompt)

    def test_missing_key_is_not_an_error(self):
        os.environ.pop("CCR_TEST_KEY", None)
        result = analysis.analyse_run(self.conn, "run1", self.cfg)
        self.assertIsNone(result["analysis"])
        self.assertIn("not set", result["reason"])
        self.assertTrue(os.path.exists(result["prompt"]))

    def test_bundle_only_skips_the_call_even_with_a_key(self):
        os.environ["CCR_TEST_KEY"] = "sk-test"
        try:
            result = analysis.analyse_run(self.conn, "run1", self.cfg,
                                          use_ai=False)
            self.assertIsNone(result["analysis"])
            self.assertIn("skipped", result["reason"])
        finally:
            os.environ.pop("CCR_TEST_KEY")

    def test_analysis_is_written_when_a_client_is_available(self):
        seen = []
        client = ai.OpenAIClient(AI_CFG, transport=fake_transport(
            "## Summary\nA small market.\n", seen))
        result = analysis.analyse_run(self.conn, "run1", self.cfg,
                                      client=client)

        self.assertTrue(os.path.exists(result["analysis"]))
        with open(result["analysis"], encoding="utf-8") as f:
            written = f.read()
        self.assertIn("A small market", written)
        self.assertIn("gpt-4o-mini", written)

        # The model must receive the dataset, not raw HTML or URLs.
        sent = seen[0][1]["messages"][1]["content"]
        self.assertIn("POS MCP Bridge", sent)
        self.assertNotIn("<html", sent)


class TestKeywordCommandWiring(unittest.TestCase):
    """The CLI is what a person actually runs, so exercise that path too."""

    def setUp(self):
        import json as _json
        self.tmp = tempfile.mkdtemp()
        self.keywords = os.path.join(self.tmp, "keywords.csv")
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            _json.dump({
                "base_url": "https://codecanyon.net",
                "ai": dict(AI_CFG, api_key_env="CCR_TEST_KEY"),
                "paths": {"db": os.path.join(self.tmp, "db", "t.sqlite"),
                          "keywords": self.keywords},
            }, f)
        keyword_file.save(self.keywords, [
            {"keyword": "ultimate pos", "parent_topic": "POS",
             "source": "manual", "approved": True, "priority": "high"}])

    def tearDown(self):
        os.environ.pop("CCR_TEST_KEY", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generate_writes_unapproved_rows_and_leaves_existing_alone(self):
        import run as cli

        os.environ["CCR_TEST_KEY"] = "sk-test"
        real_client = ai.OpenAIClient
        ai.OpenAIClient = lambda cfg: real_client(cfg, transport=fake_transport(
            '{"keywords": [{"keyword": "ultimate pos mcp", '
            '"band": "speculative", "priority": "low"}]}'))
        try:
            code = cli.main(["--quiet", "--config", self.config, "keywords",
                             "--generate", "--topic", "Ultimate POS"])
        finally:
            ai.OpenAIClient = real_client

        self.assertEqual(code, 0)
        rows = {r["keyword"]: r for r in
                keyword_file.load(self.keywords, include_unapproved=True)}
        self.assertIn("ultimate pos mcp", rows)
        self.assertFalse(rows["ultimate pos mcp"]["approved"])
        self.assertTrue(rows["ultimate pos"]["approved"])

        # Only the approved one would be crawled.
        self.assertEqual([r["keyword"] for r in
                          keyword_file.load(self.keywords)], ["ultimate pos"])

    def test_generate_without_a_key_fails_with_guidance_not_a_traceback(self):
        import run as cli
        os.environ.pop("CCR_TEST_KEY", None)
        code = cli.main(["--quiet", "--config", self.config, "keywords",
                         "--generate", "--topic", "Ultimate POS"])
        self.assertEqual(code, 1)

    def test_approve_all_via_cli(self):
        import run as cli
        keyword_file.merge(self.keywords, [
            {"keyword": "ultimate pos api", "source": "ai", "approved": False}])
        code = cli.main(["--quiet", "--config", self.config, "keywords",
                         "--approve-all"])
        self.assertEqual(code, 0)
        self.assertEqual(len(keyword_file.load(self.keywords)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
