#!/usr/bin/env python
"""Local dashboard.

  python serve.py            then open http://127.0.0.1:8765

Binds to loopback only. Reads from the SQLite database and can launch a
crawl in a background thread; progress is polled from the crawl_pages
ledger, which is already the authoritative record of what has been fetched.

Standard library only -- http.server, no framework.
"""

import argparse
import base64
import hmac
import ipaddress
import json
import mimetypes
import os
import posixpath
import sqlite3
import threading
import traceback
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from ccr import ai, analysis, diff, exporters, stats
from ccr import report as report_module
from ccr import keywords as keyword_file
from ccr.config import Config
from ccr.http_client import HttpClient
from ccr.pipeline import Crawler, new_run_id
from ccr.store import Store
from ccr.throttle import Throttle

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PROJECT_ROOT, "web")


class RunManager:
    """Owns the single background crawl, if one is running.

    One at a time on purpose: two concurrent crawls would double the request
    rate against the site, which is exactly what the pacing config exists to
    prevent.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.thread = None
        self.run_id = None
        self.status = "idle"
        self.log_lines = []

    @property
    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def _log(self, message):
        self.log_lines.append(message)
        del self.log_lines[:-400]

    def start(self, words, topic, resume=None, clear_draft=True):
        with self.lock:
            if self.busy:
                return None, "a run is already in progress"
            if not words:
                return None, "no keywords given"

            self.run_id = resume or new_run_id()
            self.status = "running"
            self.log_lines = [
                f"{'retrying in' if resume else 'run'} {self.run_id}",
                f"keywords: {', '.join(words)}"]

            self.thread = threading.Thread(
                target=self._run, args=(words, topic, clear_draft), daemon=True)
            self.thread.start()
            return self.run_id, None

    def _run(self, words, topic, clear_draft=True):
        store = None
        try:
            store = Store(self.cfg.resolve("db"))
            client = HttpClient(self.cfg,
                                etag_path=self.cfg.resolve("db") + ".etags",
                                log=self._log)
            throttle = Throttle(self.cfg["throttle"], log=self._log)
            crawler = Crawler(self.cfg, store, client, throttle,
                              log=self._log, run_id=self.run_id)
            self.status = crawler.crawl(words, topic=topic)

            exporters.export_all(store.conn, self.run_id, self.cfg.resolve("csv"))
            path = report_module.write_report(store.conn, self.run_id, self.cfg)
            self._log(f"exported CSVs and wrote {os.path.basename(path)}")

            # The keywords now belong to this run, recorded in the database
            # and exported to its own keywords.csv. Emptying the draft means
            # the next new run starts from a clean slate instead of silently
            # inheriting this one's list. Nothing is lost: "Reuse keywords"
            # copies them back.
            #
            # A retry must not do this: it re-crawls one keyword inside an
            # existing run and has nothing to do with the draft you may be
            # assembling for the next one.
            if clear_draft and self.status == "completed":
                keyword_file.save(self.cfg.resolve("keywords"), [])
                self._log("draft keyword list cleared; "
                          "reuse them from this run if you want them again")
        except Exception as exc:                     # noqa: BLE001
            self.status = "failed"
            self._log(f"ERROR: {exc}")
            self._log(traceback.format_exc(limit=3))
        finally:
            if store:
                store.close()
            self._log(f"run finished: {self.status}")

    def snapshot(self):
        return {"run_id": self.run_id, "status": self.status,
                "busy": self.busy, "log": self.log_lines[-200:]}


def credentials():
    """Optional HTTP Basic credentials, from the environment only.

    Kept out of config.json so a password is never committed to a repo.
    """
    user = os.environ.get("CCR_DASHBOARD_USER", "")
    password = os.environ.get("CCR_DASHBOARD_PASSWORD", "")
    return (user, password) if user and password else None


def is_loopback(host):
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ccr-dashboard"
    cfg = None
    manager = None
    auth = None                 # (user, password) or None

    def log_message(self, fmt, *args):
        pass        # the crawl log is the interesting one, not the HTTP log

    # ---------------------------------------------------------------- auth

    def authorized(self):
        if not self.auth:
            return True

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False

        # compare_digest on both halves so neither leaks length by timing
        return (hmac.compare_digest(user, self.auth[0])
                and hmac.compare_digest(password, self.auth[1]))

    def require_auth(self):
        body = b'{"error": "authentication required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="CodeCanyon research"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- plumbing

    def _send(self, status, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _store(self):
        store = Store(self.cfg.resolve("db"))
        store.conn.row_factory = sqlite3.Row
        return store

    @staticmethod
    def _contains(root, candidate):
        try:
            return os.path.commonpath([candidate, root]) == root
        except ValueError:              # different drives on Windows
            return False

    def _output_roots(self):
        """Directories whose contents may be served over HTTP.

        An allowlist, not merely a containment check against the research
        root: that tree also holds the SQLite database and the raw HTML
        archive, and neither belongs on an HTTP endpoint.
        """
        return [self.cfg.resolve("reports"), self.cfg.resolve("csv"),
                self.cfg.resolve("analysis")]

    def _serve_from(self, base, path, allowed=None, download=False):
        """Serve `path` resolved under `base`.

        `base` bounds resolution; `allowed`, when given, further restricts
        which subdirectories may be reached.
        """
        base = os.path.normpath(base)
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.normpath(os.path.join(base, rel))

        if not self._contains(base, full) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        if allowed and not any(self._contains(os.path.normpath(a), full)
                               for a in allowed):
            return self._send(404, {"error": "not found"})

        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(full)}"')
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        self._serve_from(WEB_DIR, path)

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        if not self.authorized():
            return self.require_auth()

        url = urlparse(self.path)
        query = parse_qs(url.query)
        route = url.path

        try:
            if route in ("/", "/index.html"):
                return self._static("/index.html")
            # Generated artefacts only: reports, CSVs and analysis bundles.
            # The database and the raw HTML archive are deliberately absent.
            if route.startswith("/view/"):
                return self._serve_from(self.cfg.resolve("root"), route[6:],
                                        allowed=self._output_roots())
            if route.startswith("/download/"):
                return self._serve_from(self.cfg.resolve("root"), route[10:],
                                        allowed=self._output_roots(),
                                        download=True)
            if not route.startswith("/api/"):
                return self._static(route)

            if route == "/api/runs":
                return self._send(200, self.api_runs())
            if route == "/api/report":
                return self._send(200, self.api_report(query))
            if route == "/api/products":
                return self._send(200, self.api_products(query))
            if route == "/api/keywords":
                return self._send(200, self.api_keywords(query))
            if route == "/api/progress":
                return self._send(200, self.api_progress())
            if route == "/api/diff":
                return self._send(200, self.api_diff(query))
            if route == "/api/outputs":
                return self._send(200, self.api_outputs(query))
            if route == "/api/ai-status":
                ai_cfg = self.cfg.get("ai", {}) or {}
                return self._send(200, {
                    "available": ai.available(ai_cfg),
                    "model": ai_cfg.get("model"),
                    "env_var": ai_cfg.get("api_key_env", "OPENAI_API_KEY")})
            return self._send(404, {"error": "unknown endpoint"})
        except Exception as exc:                     # noqa: BLE001
            return self._send(500, {"error": str(exc),
                                    "trace": traceback.format_exc(limit=3)})

    def api_runs(self):
        store = self._store()
        try:
            out = []
            for row in store.conn.execute(
                    "SELECT * FROM research_runs ORDER BY started_at DESC"):
                summary = store.run_summary(row["run_id"])
                out.append({**dict(row), **summary})
            return out
        finally:
            store.close()

    def _run_id(self, query, fall_back=True):
        """Resolve ?run=. `run=` given but empty means 'none', not 'latest'.

        The dashboard opens with nothing selected, so it needs a way to say
        "show me no run" that is distinct from "pick a sensible default".
        """
        if "run" in query:
            requested = (query.get("run") or [""])[0].strip()
            return requested or None
        if not fall_back:
            return None
        store = self._store()
        try:
            return stats.latest_run_id(store.conn)
        finally:
            store.close()

    def api_report(self, query):
        run_id = self._run_id(query)
        if not run_id:
            return {"empty": True, "message": "No run selected."}
        store = self._store()
        try:
            return stats.run_report(store.conn, run_id, self.cfg)
        finally:
            store.close()

    def api_products(self, query):
        """Products grouped under the keyword that found them."""
        run_id = self._run_id(query)
        if not run_id:
            return {"run_id": None, "groups": []}
        store = self._store()
        try:
            return {"run_id": run_id,
                    "groups": stats.products_by_keyword(store.conn, run_id)}
        finally:
            store.close()

    def api_keywords(self, query):
        """Either a run's keyword record, or the draft for the next run.

        With ?run=<id> this reports what that run searched, read from
        keyword_results. Without it, the editable draft from keywords.csv.
        The two are different things and conflating them made every run look
        like it had searched whatever the draft happens to hold today.
        """
        run_id = self._run_id(query, fall_back=False)

        if run_id:
            store = self._store()
            try:
                run = store.get_run(run_id)
                return {
                    "mode": "run",
                    "run_id": run_id,
                    "topic": run["topic"] if run else None,
                    "editable": False,
                    "keywords": [dict(r) for r in
                                 store.keywords_for_run(run_id)],
                }
            finally:
                store.close()

        try:
            draft = keyword_file.load(self.cfg.resolve("keywords"),
                                      include_unapproved=True)
        except FileNotFoundError:
            draft = []
        return {"mode": "draft", "run_id": None, "topic": None,
                "editable": True, "keywords": draft}

    def api_progress(self):
        snapshot = self.manager.snapshot()
        if snapshot["run_id"]:
            store = self._store()
            try:
                snapshot["summary"] = store.run_summary(snapshot["run_id"])
                snapshot["pages"] = [dict(r) for r in store.conn.execute(
                    "SELECT keyword, page, item_count, total_results, error "
                    "FROM crawl_pages WHERE run_id=? ORDER BY fetched_at DESC "
                    "LIMIT 12", (snapshot["run_id"],))]
            finally:
                store.close()
        return snapshot

    def api_outputs(self, query):
        """Everything this run has produced on disk, as browser-openable paths.

        Paths are returned relative to the research root so the browser can
        fetch them back through /view/ and /download/.
        """
        run_id = self._run_id(query)
        if not run_id:
            return {"run_id": None, "files": []}

        root = self.cfg.resolve("root")
        candidates = [
            ("report", os.path.join(self.cfg.resolve("reports"),
                                    f"{run_id}.html")),
        ]
        for name in ("keywords.csv", "products.csv", "search_occurrences.csv",
                     "keyword_summary.csv"):
            candidates.append(("csv", os.path.join(self.cfg.resolve("csv"),
                                                   run_id, name)))
        candidates.append(("csv", os.path.join(self.cfg.resolve("csv"),
                                               "research_runs.csv")))
        for name in ("dataset.md", "prompt.md", "analysis.md"):
            candidates.append(("analysis", os.path.join(
                self.cfg.resolve("analysis"), run_id, name)))

        files = []
        for kind, path in candidates:
            if not os.path.isfile(path):
                continue
            files.append({
                "kind": kind,
                "name": os.path.basename(path),
                "path": os.path.relpath(path, root).replace("\\", "/"),
                "bytes": os.path.getsize(path),
            })
        return {"run_id": run_id, "files": files}

    def api_diff(self, query):
        store = self._store()
        try:
            runs = [r["run_id"] for r in store.conn.execute(
                "SELECT run_id FROM research_runs ORDER BY started_at")]
            if len(runs) < 2:
                return {"empty": True,
                        "message": f"need two runs to compare, found {len(runs)}"}
            run_a = (query.get("from") or [runs[-2]])[0]
            run_b = (query.get("to") or [runs[-1]])[0]
            if run_a == run_b:
                return {"empty": True,
                        "message": "pick two different runs"}
            return diff.compare_runs(store.conn, run_a, run_b)
        except ValueError as exc:
            return {"empty": True, "message": str(exc)}
        finally:
            store.close()

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        if not self.authorized():
            return self.require_auth()

        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})

        try:
            if url.path == "/api/scrape":
                return self._send(200, self.api_scrape(payload))
            if url.path == "/api/export":
                return self._send(200, self.api_export(payload))
            if url.path == "/api/analyze":
                return self._send(200, self.api_analyze(payload))
            if url.path == "/api/keywords/generate":
                return self._send(200, self.api_generate_keywords(payload))
            if url.path == "/api/keywords/approve":
                return self._send(200, self.api_approve_keywords(payload))
            if url.path == "/api/keywords/add":
                return self._send(200, self.api_add_keyword(payload))
            if url.path == "/api/keywords/bulk":
                return self._send(200, self.api_bulk_keywords(payload))
            if url.path == "/api/keywords/delete":
                return self._send(200, self.api_delete_keyword(payload))
            if url.path == "/api/keywords/clear":
                return self._send(200, self.api_clear_keywords())
            if url.path == "/api/keywords/reuse":
                return self._send(200, self.api_reuse_keywords(payload))
            if url.path == "/api/keywords/retry":
                return self._send(200, self.api_retry_keywords(payload))
            return self._send(404, {"error": "unknown endpoint"})
        except Exception as exc:                     # noqa: BLE001
            return self._send(500, {"error": str(exc)})

    def api_scrape(self, payload):
        words = [w.strip() for w in (payload.get("keywords") or [])
                 if w and w.strip()]
        if not words:
            try:
                words = [r["keyword"] for r in keyword_file.load(
                    self.cfg.resolve("keywords"))]
            except FileNotFoundError:
                words = []

        run_id, error = self.manager.start(
            words, payload.get("topic"), payload.get("resume"))
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "run_id": run_id}

    def api_analyze(self, payload):
        store = self._store()
        try:
            run_id = payload.get("run") or stats.latest_run_id(store.conn)
            if not run_id:
                return {"ok": False, "error": "no runs yet"}
            result = analysis.analyse_run(
                store.conn, run_id, self.cfg,
                use_ai=not payload.get("bundle_only"))
            return {"ok": True, "run_id": run_id,
                    "dataset": os.path.basename(result["dataset"]),
                    "prompt": os.path.basename(result["prompt"]),
                    "dir": result["dir"],
                    "analysis": (os.path.basename(result["analysis"])
                                 if result.get("analysis") else None),
                    "reason": result.get("reason")}
        except (ai.AIError, ai.AIUnavailable) as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            store.close()

    def api_generate_keywords(self, payload):
        ai_cfg = self.cfg.get("ai", {}) or {}
        topic = (payload.get("topic") or "").strip()
        if not topic:
            return {"ok": False, "error": "a topic is required"}
        if not ai.available(ai_cfg):
            return {"ok": False,
                    "error": f"{ai_cfg.get('api_key_env', 'OPENAI_API_KEY')} "
                             f"is not set; add keywords to keywords.csv by hand"}

        path = self.cfg.resolve("keywords")
        try:
            existing = [r["keyword"] for r in
                        keyword_file.load(path, include_unapproved=True)]
        except FileNotFoundError:
            existing = []

        try:
            generated = ai.generate_keywords(
                ai.OpenAIClient(ai_cfg), topic, existing,
                payload.get("count") or ai_cfg.get("keyword_count", 14))
        except (ai.AIError, ai.AIUnavailable) as exc:
            return {"ok": False, "error": str(exc)}

        added, skipped = keyword_file.merge(path, generated)
        # Everything lands unapproved; a human decides what gets crawled.
        return {"ok": True, "added": added, "skipped": len(skipped)}

    def api_add_keyword(self, payload):
        """Add a keyword typed by a person.

        Approved by default, unlike a generated one: a human typing it into
        the box *is* the approval step.
        """
        keyword = (payload.get("keyword") or "").strip().lower()
        if not keyword:
            return {"ok": False, "error": "a keyword is required"}
        if len(keyword) > 80:
            return {"ok": False, "error": "keyword is too long"}

        path = self.cfg.resolve("keywords")
        added, skipped = keyword_file.merge(path, [{
            "keyword": keyword,
            "parent_topic": (payload.get("topic") or "").strip() or None,
            "source": "manual",
            "approved": True,
            "priority": (payload.get("priority") or "medium").strip(),
        }])
        if skipped:
            return {"ok": False, "error": f"{keyword!r} is already in the list"}
        return {"ok": True, "added": added}

    def api_bulk_keywords(self, payload):
        """Add a pasted list of keywords in one go, ready to crawl."""
        keywords = keyword_file.parse_bulk(payload.get("text") or "")
        if not keywords:
            return {"ok": False, "error": "no keywords found in that text"}

        topic = (payload.get("topic") or "").strip() or None
        added, skipped = keyword_file.merge(self.cfg.resolve("keywords"), [{
            "keyword": keyword,
            "parent_topic": topic,
            "source": "manual",
            "approved": True,      # typed by a person, so already approved
            "priority": "medium",
        } for keyword in keywords])

        return {"ok": True, "added": [r["keyword"] for r in added],
                "skipped": [r["keyword"] for r in skipped],
                "parsed": len(keywords)}

    def api_delete_keyword(self, payload):
        wanted = {k.lower() for k in (payload.get("keywords") or [])}
        if not wanted:
            return {"ok": False, "error": "nothing to remove"}

        path = self.cfg.resolve("keywords")
        rows = keyword_file.load(path, include_unapproved=True)
        kept = [r for r in rows if r["keyword"].lower() not in wanted]
        keyword_file.save(path, kept)
        return {"ok": True, "removed": len(rows) - len(kept)}

    def api_retry_keywords(self, payload):
        """Re-crawl keywords that failed, inside the run they failed in.

        This rides the ordinary resume path: pages that already succeeded are
        skipped without touching the network, and the crawl picks up from the
        page that broke.
        """
        run_id = (payload.get("run") or "").strip()
        keywords = payload.get("keywords") or []
        if payload.get("keyword"):
            keywords = [payload["keyword"]]
        keywords = [k.strip() for k in keywords if k and k.strip()]

        if not run_id:
            return {"ok": False, "error": "which run?"}

        store = self._store()
        try:
            run = store.get_run(run_id)
            if not run:
                return {"ok": False, "error": f"no such run: {run_id}"}
            failed = [r["keyword"] for r in store.failed_keywords(run_id)]
            topic = run["topic"]
        finally:
            store.close()

        if not keywords:
            keywords = failed          # retry everything that failed
        if not keywords:
            return {"ok": False, "error": "nothing failed in this run"}

        unknown = [k for k in keywords if k not in failed]
        if unknown:
            return {"ok": False,
                    "error": f"not marked failed in this run: "
                             f"{', '.join(unknown)}"}

        started, error = self.manager.start(keywords, topic, resume=run_id,
                                            clear_draft=False)
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "run_id": started, "keywords": keywords}

    def api_clear_keywords(self):
        """Empty the draft. Safe: past runs keep their own record."""
        path = self.cfg.resolve("keywords")
        try:
            removed = len(keyword_file.load(path, include_unapproved=True))
        except FileNotFoundError:
            removed = 0
        keyword_file.save(path, [])
        return {"ok": True, "removed": removed}

    def api_reuse_keywords(self, payload):
        """Copy a past run's keywords into the draft for a new run."""
        run_id = (payload.get("run") or "").strip()
        if not run_id:
            return {"ok": False, "error": "which run?"}

        store = self._store()
        try:
            run = store.get_run(run_id)
            rows = store.keywords_for_run(run_id)
        finally:
            store.close()

        if not rows:
            return {"ok": False, "error": f"{run_id} has no keywords recorded"}

        topic = (payload.get("topic")
                 or (run["topic"] if run else None))
        added, skipped = keyword_file.merge(self.cfg.resolve("keywords"), [{
            "keyword": row["keyword"],
            "parent_topic": row["parent_topic"] or topic,
            "source": row["source"] or "manual",
            "approved": True,
            "priority": row["priority"] or "medium",
        } for row in rows])

        return {"ok": True, "added": [r["keyword"] for r in added],
                "skipped": [r["keyword"] for r in skipped], "topic": topic}

    def api_approve_keywords(self, payload):
        changed = keyword_file.set_approval(
            self.cfg.resolve("keywords"),
            bool(payload.get("approved")),
            payload.get("keywords") or None)
        return {"ok": True, "changed": changed}

    def api_export(self, payload):
        store = self._store()
        try:
            run_id = payload.get("run") or stats.latest_run_id(store.conn)
            if not run_id:
                return {"ok": False, "error": "no runs yet"}
            paths = exporters.export_all(store.conn, run_id,
                                         self.cfg.resolve("csv"))
            report_path = report_module.write_report(store.conn, run_id,
                                                     self.cfg)
            return {"ok": True, "run_id": run_id,
                    "files": [os.path.basename(p) for p in paths],
                    "report": os.path.basename(report_path)}
        finally:
            store.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default loopback only)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--allow-insecure", action="store_true",
                    help="permit binding a public interface with no auth")
    args = ap.parse_args(argv)

    auth = credentials()
    public = not is_loopback(args.host)

    # The dashboard can start crawls and exposes the whole research dataset.
    # Open to a network with no password, that lets anyone who finds the port
    # scrape from this machine's IP. Refuse by default rather than trust the
    # operator to notice.
    if public and not auth and not args.allow_insecure:
        print(f"refusing to bind {args.host} with no authentication.\n"
              f"\n"
              f"This dashboard can start crawls and read all collected data.\n"
              f"Pick one:\n"
              f"  - keep it on loopback and reach it over an SSH tunnel:\n"
              f"      ssh -L {args.port}:127.0.0.1:{args.port} user@your-vps\n"
              f"  - set credentials and put TLS in front of it:\n"
              f"      export CCR_DASHBOARD_USER=you\n"
              f"      export CCR_DASHBOARD_PASSWORD='a long random string'\n"
              f"  - override deliberately with --allow-insecure\n")
        return 2

    cfg = Config.load(args.config)
    Handler.cfg = cfg
    Handler.manager = RunManager(cfg)
    Handler.auth = auth

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{args.port}"

    print(f"dashboard on {url}  (ctrl-c to stop)")
    print(f"bound to {args.host}:{args.port}"
          f"{'  [authentication on]' if auth else ''}")
    if public and not auth:
        print("WARNING: reachable from the network with no authentication")
    if public and auth:
        print("NOTE: Basic auth sends the password base64-encoded, so put TLS "
              "in front of this (nginx, Caddy) before using it over a network")

    # Only open a browser when a human is plausibly at this machine.
    if not args.no_browser and not public:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
