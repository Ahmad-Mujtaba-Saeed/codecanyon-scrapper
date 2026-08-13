"""Dashboard file-serving rules.

The dashboard serves generated artefacts so the whole workflow can be driven
from the browser. What it must NOT serve is the rest of the research tree:
the SQLite database and the raw HTML archive live there too.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve                                                     # noqa: E402
from ccr import keywords as keyword_file                          # noqa: E402
from ccr.config import Config                                     # noqa: E402
from ccr.store import Store                                       # noqa: E402


class ServeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        research = os.path.join(self.tmp, "research")
        paths = {
            "root": research,
            "raw": os.path.join(research, "raw"),
            "db": os.path.join(research, "db", "research.sqlite"),
            "csv": os.path.join(research, "csv"),
            "reports": os.path.join(research, "reports"),
            "analysis": os.path.join(research, "analysis"),
            "keywords": os.path.join(self.tmp, "keywords.csv"),
        }
        self.cfg = Config({"base_url": "https://codecanyon.net",
                           "paths": paths, "ai": {}}, path="<test>")

        for key in ("csv", "reports", "analysis", "raw"):
            os.makedirs(self.cfg.resolve(key), exist_ok=True)

        Store(self.cfg.resolve("db")).close()          # creates the database

        self.write(os.path.join(research, "reports", "r1.html"), "<h1>report</h1>")
        self.write(os.path.join(research, "csv", "r1", "products.csv"), "a,b\n1,2\n")
        self.write(os.path.join(research, "analysis", "r1", "prompt.md"), "# prompt")
        self.write(os.path.join(research, "raw", "secret.html.gz"), "raw archive")
        self.write(os.path.join(self.tmp, "config.json"), "{}")

        serve.Handler.cfg = self.cfg
        serve.Handler.manager = serve.RunManager(self.cfg)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def get(self, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestOutputsAreServed(ServeTestCase):
    def test_report_csv_and_bundle_are_reachable(self):
        for path in ("/view/reports/r1.html",
                     "/view/csv/r1/products.csv",
                     "/view/analysis/r1/prompt.md"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)

    def test_download_sets_attachment_header(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/download/csv/r1/products.csv",
                timeout=10) as r:
            self.assertIn("attachment", r.headers.get("Content-Disposition", ""))


class TestExposureLimits(ServeTestCase):
    """Regression: /view/db/research.sqlite once returned the database."""

    def test_database_is_not_served(self):
        status, _ = self.get("/view/db/research.sqlite")
        self.assertEqual(status, 404)

    def test_raw_archive_is_not_served(self):
        status, _ = self.get("/view/raw/secret.html.gz")
        self.assertEqual(status, 404)

    def test_traversal_out_of_the_research_tree_is_refused(self):
        for path in ("/view/../config.json",
                     "/download/../../config.json",
                     "/view/..%2f..%2fconfig.json",
                     "/view/reports/../db/research.sqlite"):
            status, _ = self.get(path)
            self.assertEqual(status, 404, path)

    def test_missing_file_inside_an_allowed_directory_is_404(self):
        status, _ = self.get("/view/reports/nope.html")
        self.assertEqual(status, 404)


class TestKeywordEndpoints(ServeTestCase):
    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read())

    def draft(self):
        return json.loads(self.get("/api/keywords")[1])["keywords"]

    def test_add_approve_and_delete_round_trip(self):
        added = self.post("/api/keywords/add",
                          {"keyword": "Perfex MCP", "topic": "Perfex"})
        self.assertTrue(added["ok"])
        # Typed by a person, so it arrives approved and ready to crawl.
        self.assertTrue(added["added"][0]["approved"])
        self.assertEqual(added["added"][0]["keyword"], "perfex mcp")

        self.assertEqual(len(self.draft()), 1)

        self.post("/api/keywords/approve",
                  {"keywords": ["perfex mcp"], "approved": False})
        self.assertFalse(self.draft()[0]["approved"])

        removed = self.post("/api/keywords/delete", {"keywords": ["perfex mcp"]})
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(self.draft(), [])

    def test_duplicate_add_is_rejected_not_silently_duplicated(self):
        self.post("/api/keywords/add", {"keyword": "perfex api"})
        again = self.post("/api/keywords/add", {"keyword": "perfex api"})
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_empty_keyword_is_rejected(self):
        self.assertFalse(self.post("/api/keywords/add", {"keyword": "  "})["ok"])

    def test_bulk_paste_splits_on_commas_and_newlines(self):
        res = self.post("/api/keywords/bulk", {
            "text": "perfex integration, perfex api\nperfex mcp;perfex webhook",
            "topic": "Perfex CRM"})
        self.assertTrue(res["ok"])
        self.assertEqual(sorted(res["added"]),
                         ["perfex api", "perfex integration", "perfex mcp",
                          "perfex webhook"])

        rows = {r["keyword"]: r for r in self.draft()}
        # Pasted by a person, so ready to crawl immediately.
        self.assertTrue(rows["perfex api"]["approved"])
        self.assertEqual(rows["perfex api"]["parent_topic"], "Perfex CRM")

    def test_bulk_paste_keeps_multi_word_keywords_intact(self):
        """Splitting on spaces would turn one real search into two useless
        ones, so spaces stay inside the keyword."""
        res = self.post("/api/keywords/bulk",
                        {"text": "ultimate pos integration"})
        self.assertEqual(res["added"], ["ultimate pos integration"])

    def test_bulk_paste_reports_duplicates_instead_of_adding_them(self):
        self.post("/api/keywords/bulk", {"text": "perfex api"})
        res = self.post("/api/keywords/bulk", {"text": "perfex api, perfex ai"})
        self.assertEqual(res["added"], ["perfex ai"])
        self.assertEqual(res["skipped"], ["perfex api"])

    def test_bulk_paste_with_no_keywords_is_an_error(self):
        self.assertFalse(self.post("/api/keywords/bulk", {"text": " ,,\n; "})["ok"])


class TestKeywordsAreRunScoped(ServeTestCase):
    """A run's keyword record must not be the editable draft.

    The draft changes between runs, so reading it back later would
    misreport what an old run actually searched.
    """

    def setUp(self):
        super().setUp()
        from ccr.store import Store
        store = Store(self.cfg.resolve("db"))
        store.start_run("r1", "Perfex CRM", "{}")
        store.upsert_keyword("perfex api", "Perfex CRM", "manual", True, "high")
        store.record_keyword_result("r1", "perfex api", "relevance",
                                    total_results=46, pages_crawled=2,
                                    unique_products=46)
        store.record_keyword_result("r1", "perfex mcp", "relevance",
                                    total_results=0, pages_crawled=1,
                                    unique_products=0)
        store.finish_run("r1")
        store.close()

        # The draft now holds something completely different.
        keyword_file.save(self.cfg.resolve("keywords"), [
            {"keyword": "worksuite crm", "parent_topic": "Worksuite",
             "source": "manual", "approved": True, "priority": "high"}])

    def post(self, path, payload=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read())

    def test_run_scoped_request_returns_that_runs_keywords(self):
        payload = json.loads(self.get("/api/keywords?run=r1")[1])
        self.assertEqual(payload["mode"], "run")
        self.assertEqual(payload["topic"], "Perfex CRM")
        self.assertFalse(payload["editable"])

        keywords = {k["keyword"]: k for k in payload["keywords"]}
        self.assertEqual(set(keywords), {"perfex api", "perfex mcp"})
        self.assertNotIn("worksuite crm", keywords,
                         "the draft must not leak into a run's record")
        self.assertEqual(keywords["perfex api"]["total_results"], 46)
        self.assertEqual(keywords["perfex mcp"]["zero_result"], 1)

    def test_unscoped_request_returns_the_editable_draft(self):
        payload = json.loads(self.get("/api/keywords")[1])
        self.assertEqual(payload["mode"], "draft")
        self.assertTrue(payload["editable"])
        self.assertEqual([k["keyword"] for k in payload["keywords"]],
                         ["worksuite crm"])

    def test_clearing_the_draft_leaves_the_run_record_intact(self):
        self.assertEqual(self.post("/api/keywords/clear")["removed"], 1)

        self.assertEqual(json.loads(self.get("/api/keywords")[1])["keywords"], [])
        still_there = json.loads(self.get("/api/keywords?run=r1")[1])
        self.assertEqual(len(still_there["keywords"]), 2)

    def test_reuse_copies_a_runs_keywords_into_the_draft(self):
        self.post("/api/keywords/clear")
        res = self.post("/api/keywords/reuse", {"run": "r1"})

        self.assertTrue(res["ok"])
        self.assertEqual(sorted(res["added"]), ["perfex api", "perfex mcp"])
        self.assertEqual(res["topic"], "Perfex CRM")

        draft = json.loads(self.get("/api/keywords")[1])["keywords"]
        self.assertEqual(sorted(k["keyword"] for k in draft),
                         ["perfex api", "perfex mcp"])
        # Copied for a run you are about to start, so ready to go.
        self.assertTrue(all(k["approved"] for k in draft))

    def test_reuse_of_an_unknown_run_is_an_error(self):
        self.assertFalse(self.post("/api/keywords/reuse", {"run": "nope"})["ok"])

    def test_reuse_without_a_run_is_an_error(self):
        self.assertFalse(self.post("/api/keywords/reuse", {})["ok"])


class TestRetryEndpoint(ServeTestCase):
    def setUp(self):
        super().setUp()
        store = Store(self.cfg.resolve("db"))
        store.start_run("r1", "Perfex CRM", "{}")
        store.record_keyword_result("r1", "perfex api", "relevance",
                                    total_results=46, pages_crawled=2,
                                    unique_products=30, status="failed",
                                    error="page 3: HTTP 503")
        store.record_keyword_result("r1", "perfex mcp", "relevance",
                                    total_results=2, pages_crawled=1,
                                    unique_products=2)
        store.finish_run("r1")
        store.close()

    def post(self, path, payload=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read())

    def test_failed_status_is_visible_in_the_run_record(self):
        rows = {k["keyword"]: k for k in
                json.loads(self.get("/api/keywords?run=r1")[1])["keywords"]}
        self.assertEqual(rows["perfex api"]["status"], "failed")
        self.assertEqual(rows["perfex api"]["error"], "page 3: HTTP 503")
        self.assertEqual(rows["perfex mcp"]["status"], "completed")

    def test_retry_without_a_keyword_retries_everything_that_failed(self):
        res = self.post("/api/keywords/retry", {"run": "r1"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["keywords"], ["perfex api"])
        self.assertEqual(res["run_id"], "r1", "retries stay in the same run")

    def test_retry_refuses_a_keyword_that_did_not_fail(self):
        res = self.post("/api/keywords/retry",
                        {"run": "r1", "keywords": ["perfex mcp"]})
        self.assertFalse(res["ok"])
        self.assertIn("not marked failed", res["error"])

    def test_retry_needs_a_run(self):
        self.assertFalse(self.post("/api/keywords/retry", {})["ok"])

    def test_retry_of_an_unknown_run_is_rejected(self):
        res = self.post("/api/keywords/retry", {"run": "nope"})
        self.assertFalse(res["ok"])
        self.assertIn("no such run", res["error"])

    def test_retry_does_not_clear_the_draft_keyword_list(self):
        """A retry re-crawls inside an old run; it has nothing to do with the
        list being assembled for the next one."""
        keyword_file.save(self.cfg.resolve("keywords"), [
            {"keyword": "worksuite crm", "parent_topic": "Worksuite",
             "source": "manual", "approved": True, "priority": "high"}])

        self.post("/api/keywords/retry", {"run": "r1"})
        for _ in range(40):
            if not serve.Handler.manager.busy:
                break
            time.sleep(0.1)

        draft = json.loads(self.get("/api/keywords")[1])["keywords"]
        self.assertEqual([k["keyword"] for k in draft], ["worksuite crm"])


class TestProductsEndpoint(ServeTestCase):
    def test_no_run_selected_returns_empty_groups(self):
        status, body = self.get("/api/products?run=")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["run_id"])
        self.assertEqual(payload["groups"], [])

    def test_report_with_explicit_empty_run_is_empty(self):
        payload = json.loads(self.get("/api/report?run=")[1])
        self.assertTrue(payload["empty"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
