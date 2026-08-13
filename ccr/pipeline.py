"""Crawl orchestration.

One keyword at a time, one page at a time, strictly sequential. Pagination
stops on <link rel="next">, never on a guess. Every page attempt is written
to the crawl_pages ledger before the next one starts, which is what makes an
interrupted run resumable: rerun with the same run id and finished pages are
skipped without touching the network.
"""

import datetime
import os

from . import parser
from .archive import RawArchive
from .http_client import AbortRun
from .robots import fetch_rules, RobotsRules
from .store import Store, utcnow
from .urls import search_url


def new_run_id():
    return datetime.datetime.now().strftime("%Y%m%dT%H%M%S")


class Crawler:
    def __init__(self, cfg, store, client, throttle, log, run_id=None,
                 max_pages=None):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.throttle = throttle
        self.log = log

        self.run_id = run_id or new_run_id()
        self.run_date = datetime.date.today().isoformat()
        self.archive = RawArchive(cfg.resolve("raw"), self.run_id, self.run_date)
        self.max_pages = max_pages or cfg.max_pages_per_keyword

        self.robots = None
        self._first_request = True

        self.pages_fetched = 0
        self.pages_skipped = 0
        self.pages_failed = 0

    # --------------------------------------------------------------- robots

    def load_robots(self):
        if not self.cfg.respect_robots:
            self.log("robots.txt: ignored by config (respect_robots=false)")
            self.robots = RobotsRules.permissive()
            return
        self.robots = fetch_rules(self.client, self.cfg.base_url)
        self.log(f"robots.txt: loaded {len(self.robots._rules)} rules "
                 f"for User-agent: *")

    def _robots_block(self, url):
        """Returns the blocking pattern, or None if the URL is crawlable."""
        if self.robots is None or self.robots.allowed(url):
            return None
        return self.robots.reason(url) or "disallowed"

    # ------------------------------------------------------------ one page

    def _fetch_page(self, keyword, sort, page, referer):
        url = search_url(self.cfg.base_url, keyword, sort, page)

        blocked_by = self._robots_block(url)
        if blocked_by:
            result = parser.PageResult(keyword=keyword, sort=sort, page=page,
                                       url=url)
            result.error = f"blocked by robots.txt rule {blocked_by}"
            result.fetched_at = utcnow()
            self.log(f"  x robots.txt blocks {url} ({blocked_by})")
            return result

        self.throttle.before_page(is_first_request=self._first_request)
        self._first_request = False

        status, body, headers = self.client.get(
            url, referer=referer, throttle=self.throttle)
        self.throttle.after_page()

        result = parser.PageResult(keyword=keyword, sort=sort, page=page,
                                   url=url)
        result.http_status = status
        result.fetched_at = utcnow()

        if status == 304:
            previous = self.store.last_raw_path_for_url(url)
            if previous and RawArchive.exists(previous):
                self.log(f"  = 304 unchanged, reusing {os.path.basename(previous)}")
                body = RawArchive.load(previous)
                result.from_cache = True
                status = result.http_status = 200
            else:
                result.error = "304 but no archived copy to fall back on"
                return result

        if status != 200 or not body:
            result.error = result.error or f"HTTP {status}"
            return result

        result.raw_path = self.archive.save(keyword, sort, page, body)

        parsed = parser.parse_search_page(
            body, keyword=keyword, sort=sort, page=page, url=url,
            items_per_page=self.cfg["items_per_page_expected"],
            min_success_ratio=self.cfg["parser"]["min_parse_success_ratio"])

        parsed.http_status = 200
        parsed.fetched_at = result.fetched_at
        parsed.raw_path = result.raw_path
        parsed.from_cache = result.from_cache
        return parsed

    # --------------------------------------------------------- one keyword

    def crawl_keyword(self, keyword, sort="relevance"):
        self.log(f"keyword: {keyword!r} [{sort}]")

        page = 1
        referer = None
        pages_crawled = 0
        total_results = None
        product_ids = set()

        while page <= self.max_pages:
            url = search_url(self.cfg.base_url, keyword, sort, page)

            # Resume: this page already succeeded in this run, so its rows
            # are in the database. Read the ledger instead of the network.
            if self.store.page_already_done(self.run_id, keyword, sort, page):
                row = self.store.get_page_row(self.run_id, keyword, sort, page)
                self.log(f"  - page {page} already done, skipping")
                self.pages_skipped += 1
                pages_crawled += 1
                total_results = row["total_results"]
                referer = url
                if not row["has_next"]:
                    break
                page += 1
                continue

            result = self._fetch_page(keyword, sort, page, referer)
            self.store.record_page(self.run_id, result)

            if result.error:
                self.pages_failed += 1
                self.log(f"  ! page {page} failed: {result.error}")
                break

            self.pages_fetched += 1
            pages_crawled += 1
            if result.total_results is not None:
                total_results = result.total_results

            self.store.record_products(self.run_id, result.products)
            self.store.record_occurrences(self.run_id, result.occurrences)
            product_ids.update(p.product_id for p in result.products)

            self.log(f"  + page {page}: {len(result.products)} items"
                     f"{'' if total_results is None else f' of {total_results}'}"
                     f"{' (next)' if result.has_next else ' (last page)'}")

            referer = url
            if not result.has_next:
                break
            page += 1
        else:
            self.log(f"  ! hit max_pages_per_keyword={self.max_pages}")

        # Counts come from storage, not from the in-memory set: on a resumed
        # run most pages are skipped and parse nothing, so the set would be
        # empty and every keyword would be misfiled as zero-result.
        unique_products = self.store.count_unique_products_for_keyword(
            self.run_id, keyword, sort)
        if total_results is None:
            total_results = self.store.total_results_for_keyword(
                self.run_id, keyword, sort)

        # A keyword returning nothing is a finding, not a failure to record
        # (spec sections 21 and 22).
        self.store.record_keyword_result(
            self.run_id, keyword, sort,
            total_results=total_results if total_results is not None else 0,
            pages_crawled=pages_crawled,
            unique_products=unique_products)

        if not unique_products:
            self.log(f"  = zero results for {keyword!r} - recorded as a "
                     f"competition signal")

        return unique_products

    # -------------------------------------------------------------- a run

    def crawl(self, keywords, sorts=None, topic=None):
        sorts = sorts or self.cfg.sorts
        existing = self.store.get_run(self.run_id)

        if existing:
            self.log(f"resuming run {self.run_id}")
        else:
            self.store.start_run(self.run_id, topic, self.cfg.snapshot())
            self.log(f"run {self.run_id} started"
                     f"{f' (topic: {topic})' if topic else ''}")

        self.load_robots()
        status = "completed"

        pairs = [(k, s) for k in keywords for s in sorts]

        try:
            for index, (keyword, sort) in enumerate(pairs):
                before = self.pages_fetched
                self.crawl_keyword(keyword, sort)
                # Only pace between keywords if the last one actually hit the
                # network. A fully resumed run has nothing to be polite about.
                if index < len(pairs) - 1 and self.pages_fetched > before:
                    self.throttle.between_keywords()
        except AbortRun as e:
            status = "aborted"
            self.log(f"ABORTED: {e}")
        except parser.ParserHealthError as e:
            status = "aborted"
            self.log(f"ABORTED: {e}")
        except KeyboardInterrupt:
            status = "aborted"
            self.log("interrupted by user; run is resumable with the same id")

        self.store.finish_run(self.run_id, status)
        return status
