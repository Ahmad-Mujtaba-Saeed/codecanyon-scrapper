"""HTTP layer: one connection, real browser headers, polite failure.

Notes on the header set. A browser sends a *consistent* fingerprint for a
whole session, so the user agent is fixed for the run rather than rotated
per request -- rotation is itself a detection signal. The referer chain is
maintained (page N cites page N-1) because a human clicking "next page"
produces exactly that, and Sec-Fetch-* is sent because Chrome always does.

Cloudflare hands out a __cf_bm cookie; the cookie jar keeps it for the
session, as a browser would.
"""

import gzip
import io
import json
import os
import time
import zlib
import http.cookiejar
import urllib.error
import urllib.request


class AbortRun(Exception):
    """Raised when the site is clearly unhappy and we should stop entirely."""


class Blocked(Exception):
    """Raised when robots.txt forbids a URL."""


class HttpClient:
    def __init__(self, cfg, etag_path=None, log=None, sleep=time.sleep):
        http_cfg = cfg["http"]
        self.user_agent = http_cfg["user_agent"]
        self.timeout = http_cfg["timeout"]
        self.max_retries = http_cfg["max_retries"]
        self.backoff_base = http_cfg["backoff_base"]
        self.backoff_max = http_cfg["backoff_max"]
        self.failure_limit = http_cfg["consecutive_failure_abort"]

        self._log = log or (lambda msg: None)
        self._sleep = sleep

        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            # Redirects are followed by default; we just want no auto-retry.
        )

        self.consecutive_failures = 0
        self.requests_made = 0
        self.last_url = None

        self._etag_path = etag_path
        self._etags = {}
        if etag_path and os.path.exists(etag_path):
            try:
                with open(etag_path, encoding="utf-8") as f:
                    self._etags = json.load(f)
            except (ValueError, OSError):
                self._etags = {}

    # ------------------------------------------------------------- headers

    def _headers(self, url, referer=None, etag=None):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if referer:
            headers["Referer"] = referer
        if etag:
            headers["If-None-Match"] = etag
        return headers

    @staticmethod
    def _decode(body, encoding):
        if not body:
            return body
        encoding = (encoding or "").lower()
        if "gzip" in encoding:
            return gzip.GzipFile(fileobj=io.BytesIO(body)).read()
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        return body

    # --------------------------------------------------------------- fetch

    def get_raw(self, url, referer=None):
        """Single attempt, no retries. Returns (status, body, headers)."""
        req = urllib.request.Request(
            url, headers=self._headers(url, referer))
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = self._decode(resp.read(), resp.headers.get(
                    "Content-Encoding"))
                return resp.status, body, dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = self._decode(e.read(), e.headers.get("Content-Encoding"))
            except Exception:
                pass
            return e.code, body, dict(e.headers or {})

    def get(self, url, referer=None, throttle=None, use_etag=True):
        """Fetch with retries and backoff.

        Returns (status, body, headers). A 304 comes back with an empty body
        and the caller is expected to fall back to the archived copy.
        """
        etag = self._etags.get(url) if use_etag else None
        attempt = 0

        while True:
            try:
                req = urllib.request.Request(
                    url, headers=self._headers(url, referer, etag))
                with self.opener.open(req, timeout=self.timeout) as resp:
                    status = resp.status
                    headers = dict(resp.headers)
                    body = self._decode(
                        resp.read(), resp.headers.get("Content-Encoding"))
            except urllib.error.HTTPError as e:
                status = e.code
                headers = dict(e.headers or {})
                body = b""
                if status == 304:
                    pass    # not an error: content unchanged
                elif status in (429, 503) or status >= 500:
                    attempt += 1
                    self._retry_or_abort(attempt, url, f"HTTP {status}",
                                         headers, throttle)
                    continue
                else:
                    self._register_failure()
                    return status, b"", headers
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                attempt += 1
                self._retry_or_abort(attempt, url, repr(e), {}, throttle)
                continue

            self.requests_made += 1
            self.last_url = url
            self.consecutive_failures = 0

            if status == 200 and headers.get("ETag"):
                self._etags[url] = headers["ETag"]
                self._save_etags()

            return status, body, headers

    # ------------------------------------------------------------ failures

    def _retry_or_abort(self, attempt, url, why, headers, throttle):
        if attempt > self.max_retries:
            self._register_failure()
            raise AbortRun(
                f"gave up on {url} after {self.max_retries} retries ({why})")

        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                delay = min(self.backoff_max, float(retry_after))
                self._log(f"  ! {why}; honoring Retry-After {delay}s")
                self._sleep(delay)
                return
            except ValueError:
                pass

        self._log(f"  ! {why}; backing off (attempt {attempt})")
        if throttle:
            throttle.backoff(attempt - 1, self.backoff_base, self.backoff_max)
        else:
            self._sleep(min(self.backoff_max,
                            self.backoff_base * (2 ** (attempt - 1))))

    def _register_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_limit:
            raise AbortRun(
                f"{self.consecutive_failures} consecutive failures; stopping "
                f"rather than hammering the site")

    # --------------------------------------------------------------- etags

    def _save_etags(self):
        if not self._etag_path:
            return
        os.makedirs(os.path.dirname(self._etag_path), exist_ok=True)
        tmp = self._etag_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._etags, f, indent=1)
        os.replace(tmp, self._etag_path)
