"""Binding and authentication rules for deployment.

The dashboard can start crawls and exposes the whole research dataset, so
the interface it binds and whether it demands a password are safety
decisions, not preferences.
"""

import base64
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


class TestLoopbackDetection(unittest.TestCase):
    def test_loopback_addresses(self):
        for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost", ""):
            self.assertTrue(serve.is_loopback(host), host)

    def test_public_addresses(self):
        for host in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.4", "203.0.113.7"):
            self.assertFalse(serve.is_loopback(host), host)

    def test_hostname_is_not_assumed_loopback(self):
        self.assertFalse(serve.is_loopback("example.com"))


class TestCredentials(unittest.TestCase):
    def setUp(self):
        for key in ("CCR_DASHBOARD_USER", "CCR_DASHBOARD_PASSWORD"):
            os.environ.pop(key, None)

    tearDown = setUp

    def test_absent_by_default(self):
        self.assertIsNone(serve.credentials())

    def test_both_halves_required(self):
        os.environ["CCR_DASHBOARD_USER"] = "you"
        self.assertIsNone(serve.credentials())
        os.environ["CCR_DASHBOARD_PASSWORD"] = "secret"
        self.assertEqual(serve.credentials(), ("you", "secret"))


class TestBindRefusal(unittest.TestCase):
    """A public bind with no password must not start."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"base_url": "https://codecanyon.net", "ai": {},
                       "paths": {"root": self.tmp,
                                 "db": os.path.join(self.tmp, "t.sqlite"),
                                 "csv": self.tmp, "reports": self.tmp,
                                 "analysis": self.tmp,
                                 "keywords": os.path.join(self.tmp, "k.csv")}}, f)
        for key in ("CCR_DASHBOARD_USER", "CCR_DASHBOARD_PASSWORD"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key in ("CCR_DASHBOARD_USER", "CCR_DASHBOARD_PASSWORD"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_public_bind_without_auth_is_refused(self):
        code = serve.main(["--host", "0.0.0.0", "--port", "0",
                           "--config", self.config, "--no-browser"])
        self.assertEqual(code, 2)

    def test_refusal_happens_before_the_config_is_even_loaded(self):
        # A missing config must not mask the safety check.
        code = serve.main(["--host", "0.0.0.0", "--port", "0",
                           "--config", os.path.join(self.tmp, "missing.json"),
                           "--no-browser"])
        self.assertEqual(code, 2)


class AuthServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        research = os.path.join(self.tmp, "research")
        paths = {"root": research, "raw": os.path.join(research, "raw"),
                 "db": os.path.join(research, "db", "r.sqlite"),
                 "csv": os.path.join(research, "csv"),
                 "reports": os.path.join(research, "reports"),
                 "analysis": os.path.join(research, "analysis"),
                 "keywords": os.path.join(self.tmp, "keywords.csv")}
        cfg = Config({"base_url": "https://codecanyon.net", "paths": paths,
                      "ai": {}}, path="<test>")
        for key in ("csv", "reports", "analysis", "raw"):
            os.makedirs(cfg.resolve(key), exist_ok=True)
        Store(cfg.resolve("db")).close()

        serve.Handler.cfg = cfg
        serve.Handler.manager = serve.RunManager(cfg)
        serve.Handler.auth = ("admin", "correct-horse")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        serve.Handler.auth = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, path, user=None, password=None, method="GET", body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if user is not None:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestAuthEnforcement(AuthServerTestCase):
    def test_no_credentials_is_401(self):
        status, _ = self.request("/api/runs")
        self.assertEqual(status, 401)

    def test_wrong_password_is_401(self):
        status, _ = self.request("/api/runs", "admin", "wrong")
        self.assertEqual(status, 401)

    def test_wrong_user_is_401(self):
        status, _ = self.request("/api/runs", "someone", "correct-horse")
        self.assertEqual(status, 401)

    def test_correct_credentials_pass(self):
        status, body = self.request("/api/runs", "admin", "correct-horse")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_challenge_header_is_sent(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)
            self.assertIn("Basic", e.headers.get("WWW-Authenticate", ""))

    def test_the_page_itself_is_protected(self):
        self.assertEqual(self.request("/")[0], 401)
        self.assertEqual(self.request("/app.js")[0], 401)

    def test_generated_files_are_protected(self):
        self.assertEqual(self.request("/view/reports/x.html")[0], 401)
        self.assertEqual(self.request("/download/csv/x.csv")[0], 401)

    def test_post_endpoints_are_protected(self):
        """Crucially, starting a crawl must not be reachable unauthenticated."""
        status, _ = self.request("/api/scrape", method="POST",
                                 body={"topic": "x"})
        self.assertEqual(status, 401)
        self.assertFalse(serve.Handler.manager.busy)

    def test_malformed_authorization_header_is_rejected(self):
        for header in ("Basic !!!!", "Bearer token", "Basic ", "nonsense"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/runs")
            req.add_header("Authorization", header)
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail(f"expected 401 for {header!r}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401, header)


class TestNoAuthConfigured(AuthServerTestCase):
    def setUp(self):
        super().setUp()
        serve.Handler.auth = None

    def test_everything_open_when_no_credentials_are_set(self):
        status, _ = self.request("/api/runs")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
