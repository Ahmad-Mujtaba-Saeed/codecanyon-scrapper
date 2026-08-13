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
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve                                                     # noqa: E402
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

    def test_add_approve_and_delete_round_trip(self):
        added = self.post("/api/keywords/add",
                          {"keyword": "Perfex MCP", "topic": "Perfex"})
        self.assertTrue(added["ok"])
        # Typed by a person, so it arrives approved and ready to crawl.
        self.assertTrue(added["added"][0]["approved"])
        self.assertEqual(added["added"][0]["keyword"], "perfex mcp")

        rows = json.loads(self.get("/api/keywords")[1])
        self.assertEqual(len(rows), 1)

        self.post("/api/keywords/approve",
                  {"keywords": ["perfex mcp"], "approved": False})
        rows = json.loads(self.get("/api/keywords")[1])
        self.assertFalse(rows[0]["approved"])

        removed = self.post("/api/keywords/delete", {"keywords": ["perfex mcp"]})
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(json.loads(self.get("/api/keywords")[1]), [])

    def test_duplicate_add_is_rejected_not_silently_duplicated(self):
        self.post("/api/keywords/add", {"keyword": "perfex api"})
        again = self.post("/api/keywords/add", {"keyword": "perfex api"})
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_empty_keyword_is_rejected(self):
        self.assertFalse(self.post("/api/keywords/add", {"keyword": "  "})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
