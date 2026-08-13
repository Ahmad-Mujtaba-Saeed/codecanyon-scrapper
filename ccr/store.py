"""SQLite persistence.

SQLite is the source of truth; the CSVs in section 15 of the spec are
generated from it. That buys three things a pile of CSVs cannot:
resume-after-interrupt, cross-run diffing, and an audit trail of which run
first saw a product.
"""

import os
import sqlite3
import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id        TEXT PRIMARY KEY,
    topic         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running | completed | aborted
    config_json   TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS keywords (
    keyword       TEXT PRIMARY KEY,
    parent_topic  TEXT,
    source        TEXT,                   -- ai | manual
    approved      INTEGER DEFAULT 1,
    priority      TEXT,
    added_at      TEXT
);

-- The resume ledger. One row per page we attempted, successful or not.
CREATE TABLE IF NOT EXISTS crawl_pages (
    run_id        TEXT NOT NULL,
    keyword       TEXT NOT NULL,
    sort          TEXT NOT NULL,
    page          INTEGER NOT NULL,
    url           TEXT NOT NULL,
    http_status   INTEGER,
    fetched_at    TEXT,
    raw_path      TEXT,
    item_count    INTEGER,
    total_results INTEGER,
    has_next      INTEGER,
    parse_ratio   REAL,
    error         TEXT,
    PRIMARY KEY (run_id, keyword, sort, page)
);

-- One row per unique product, holding the most recently seen values.
CREATE TABLE IF NOT EXISTS products (
    product_id       TEXT PRIMARY KEY,
    title            TEXT,
    url              TEXT,
    author_name      TEXT,
    author_url       TEXT,
    category         TEXT,
    subcategory      TEXT,
    price            REAL,
    sales            INTEGER,
    rating           REAL,
    review_count     INTEGER,
    software_version TEXT,
    framework        TEXT,
    compatible_with  TEXT,
    file_types       TEXT,
    last_updated     TEXT,
    first_seen_run   TEXT,
    last_seen_run    TEXT,
    first_seen_at    TEXT,
    last_seen_at     TEXT
);

-- Per-run values, so "20 sales in May, 143 sales in August" is answerable.
CREATE TABLE IF NOT EXISTS product_snapshots (
    run_id       TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    price        REAL,
    sales        INTEGER,
    rating       REAL,
    review_count INTEGER,
    last_updated TEXT,
    scraped_at   TEXT,
    PRIMARY KEY (run_id, product_id)
);

-- Where and how often each product appeared (spec section 13).
CREATE TABLE IF NOT EXISTS occurrences (
    run_id     TEXT NOT NULL,
    product_id TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    sort       TEXT NOT NULL,
    page       INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    scraped_at TEXT,
    PRIMARY KEY (run_id, product_id, keyword, sort, page, position)
);

-- Per keyword outcome. Zero-result keywords are recorded, never dropped
-- (spec sections 21 and 22).
CREATE TABLE IF NOT EXISTS keyword_results (
    run_id          TEXT NOT NULL,
    keyword         TEXT NOT NULL,
    sort            TEXT NOT NULL,
    total_results   INTEGER,
    pages_crawled   INTEGER,
    unique_products INTEGER,
    zero_result     INTEGER,
    completed_at    TEXT,
    status          TEXT,       -- completed | failed
    error           TEXT,
    PRIMARY KEY (run_id, keyword, sort)
);

CREATE INDEX IF NOT EXISTS idx_occ_product ON occurrences(product_id);
CREATE INDEX IF NOT EXISTS idx_occ_keyword ON occurrences(keyword);
CREATE INDEX IF NOT EXISTS idx_snap_product ON product_snapshots(product_id);
"""


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Bring an older database up to the current schema.

        CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so
        columns added later have to be applied by hand. Databases from
        earlier runs are worth keeping -- they hold the history the whole
        cross-run comparison depends on.
        """
        columns = {row["name"] for row in
                   self.conn.execute("PRAGMA table_info(keyword_results)")}
        for name, ddl in (("status", "TEXT"), ("error", "TEXT")):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE keyword_results ADD COLUMN {name} {ddl}")

        # Rows written before the column existed finished normally, or the
        # run would have been marked aborted.
        self.conn.execute(
            "UPDATE keyword_results SET status='completed' WHERE status IS NULL")

    def close(self):
        self.conn.close()

    # ---------------------------------------------------------------- runs

    def start_run(self, run_id, topic, config_json, note=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO research_runs "
            "(run_id, topic, started_at, status, config_json, note) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, topic, utcnow(), "running", config_json, note),
        )
        self.conn.commit()

    def finish_run(self, run_id, status="completed"):
        self.conn.execute(
            "UPDATE research_runs SET finished_at=?, status=? WHERE run_id=?",
            (utcnow(), status, run_id),
        )
        self.conn.commit()

    def get_run(self, run_id):
        cur = self.conn.execute(
            "SELECT * FROM research_runs WHERE run_id=?", (run_id,))
        return cur.fetchone()

    def latest_unfinished_run(self):
        cur = self.conn.execute(
            "SELECT * FROM research_runs WHERE status='running' "
            "ORDER BY started_at DESC LIMIT 1")
        return cur.fetchone()

    # ------------------------------------------------------------ keywords

    def upsert_keyword(self, keyword, parent_topic=None, source="manual",
                       approved=True, priority=None):
        self.conn.execute(
            "INSERT INTO keywords (keyword, parent_topic, source, approved, "
            "priority, added_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(keyword) DO UPDATE SET "
            "parent_topic=excluded.parent_topic, source=excluded.source, "
            "approved=excluded.approved, priority=excluded.priority",
            (keyword, parent_topic, source, 1 if approved else 0,
             priority, utcnow()),
        )
        self.conn.commit()

    def keywords_for_run(self, run_id):
        """What a particular run actually searched.

        keyword_results is the authoritative record: keywords.csv is a
        working draft that changes between runs, so it cannot answer "what
        did this run search" after the fact.
        """
        return self.conn.execute(
            "SELECT kr.keyword, kr.total_results, kr.unique_products, "
            "kr.zero_result, kr.pages_crawled, kr.status, kr.error, "
            "k.parent_topic, k.source, k.priority FROM keyword_results kr "
            "LEFT JOIN keywords k ON k.keyword = kr.keyword "
            "WHERE kr.run_id=? ORDER BY kr.total_results DESC, kr.keyword",
            (run_id,),
        ).fetchall()

    # --------------------------------------------------------- resume gate

    def page_already_done(self, run_id, keyword, sort, page):
        """True if this page was fetched successfully in this run already."""
        cur = self.conn.execute(
            "SELECT http_status, error FROM crawl_pages "
            "WHERE run_id=? AND keyword=? AND sort=? AND page=?",
            (run_id, keyword, sort, page),
        )
        row = cur.fetchone()
        return bool(row and row["error"] is None and row["http_status"] == 200)

    def get_page_row(self, run_id, keyword, sort, page):
        cur = self.conn.execute(
            "SELECT * FROM crawl_pages "
            "WHERE run_id=? AND keyword=? AND sort=? AND page=?",
            (run_id, keyword, sort, page),
        )
        return cur.fetchone()

    def last_raw_path_for_url(self, url):
        """Most recent archived copy of a URL, across all runs.

        Used when the server answers a conditional request with 304: the
        content is unchanged, so the previous archive is still accurate.
        """
        cur = self.conn.execute(
            "SELECT raw_path FROM crawl_pages WHERE url=? AND raw_path "
            "IS NOT NULL AND error IS NULL ORDER BY fetched_at DESC LIMIT 1",
            (url,),
        )
        row = cur.fetchone()
        return row["raw_path"] if row else None

    def record_page(self, run_id, result):
        self.conn.execute(
            "INSERT OR REPLACE INTO crawl_pages (run_id, keyword, sort, page, "
            "url, http_status, fetched_at, raw_path, item_count, "
            "total_results, has_next, parse_ratio, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, result.keyword, result.sort, result.page, result.url,
             result.http_status, result.fetched_at, result.raw_path,
             len(result.products), result.total_results,
             1 if result.has_next else 0, result.parse_ratio, result.error),
        )
        self.conn.commit()

    # --------------------------------------------------------- product data

    def record_products(self, run_id, products, scraped_at=None):
        scraped_at = scraped_at or utcnow()
        for p in products:
            self.conn.execute(
                "INSERT INTO products (product_id, title, url, author_name, "
                "author_url, category, subcategory, price, sales, rating, "
                "review_count, software_version, framework, compatible_with, "
                "file_types, last_updated, first_seen_run, last_seen_run, "
                "first_seen_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(product_id) DO UPDATE SET "
                "title=excluded.title, url=excluded.url, "
                "author_name=excluded.author_name, "
                "author_url=excluded.author_url, "
                "category=excluded.category, subcategory=excluded.subcategory, "
                "price=excluded.price, sales=excluded.sales, "
                "rating=excluded.rating, review_count=excluded.review_count, "
                "software_version=excluded.software_version, "
                "framework=excluded.framework, "
                "compatible_with=excluded.compatible_with, "
                "file_types=excluded.file_types, "
                "last_updated=excluded.last_updated, "
                "last_seen_run=excluded.last_seen_run, "
                "last_seen_at=excluded.last_seen_at",
                (p.product_id, p.title, p.url, p.author_name, p.author_url,
                 p.category, p.subcategory, p.price, p.sales, p.rating,
                 p.review_count, p.software_version, p.framework,
                 p.compatible_with, p.file_types, p.last_updated,
                 run_id, run_id, scraped_at, scraped_at),
            )
            # One snapshot per product per run: first sighting wins, so a
            # product seen under five keywords does not get five rows.
            self.conn.execute(
                "INSERT OR IGNORE INTO product_snapshots (run_id, product_id, "
                "price, sales, rating, review_count, last_updated, scraped_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, p.product_id, p.price, p.sales, p.rating,
                 p.review_count, p.last_updated, scraped_at),
            )
        self.conn.commit()

    def record_occurrences(self, run_id, occurrences, scraped_at=None):
        scraped_at = scraped_at or utcnow()
        self.conn.executemany(
            "INSERT OR REPLACE INTO occurrences (run_id, product_id, keyword, "
            "sort, page, position, scraped_at) VALUES (?,?,?,?,?,?,?)",
            [(run_id, o.product_id, o.keyword, o.sort, o.page, o.position,
              scraped_at) for o in occurrences],
        )
        self.conn.commit()

    def count_unique_products_for_keyword(self, run_id, keyword, sort):
        """Distinct products recorded for a keyword, read back from storage.

        Derived from the occurrences table rather than from whatever the
        crawler happens to hold in memory, so a resumed run -- where most
        pages are skipped and parse nothing -- still reports true counts.
        """
        cur = self.conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM occurrences "
            "WHERE run_id=? AND keyword=? AND sort=?",
            (run_id, keyword, sort),
        )
        return cur.fetchone()[0]

    def total_results_for_keyword(self, run_id, keyword, sort):
        """Result count as reported by the site, from the page ledger."""
        cur = self.conn.execute(
            "SELECT total_results FROM crawl_pages WHERE run_id=? AND "
            "keyword=? AND sort=? AND total_results IS NOT NULL "
            "ORDER BY page LIMIT 1",
            (run_id, keyword, sort),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def record_keyword_result(self, run_id, keyword, sort, total_results,
                              pages_crawled, unique_products,
                              status="completed", error=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO keyword_results (run_id, keyword, sort, "
            "total_results, pages_crawled, unique_products, zero_result, "
            "completed_at, status, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, keyword, sort, total_results, pages_crawled,
             unique_products, 1 if not unique_products else 0, utcnow(),
             status, error),
        )
        self.conn.commit()

    def failed_keywords(self, run_id):
        return self.conn.execute(
            "SELECT keyword, sort, error, pages_crawled, unique_products "
            "FROM keyword_results WHERE run_id=? AND status='failed' "
            "ORDER BY keyword", (run_id,)).fetchall()

    # ------------------------------------------------------------- reading

    def run_summary(self, run_id):
        c = self.conn
        return {
            "pages": c.execute(
                "SELECT COUNT(*) FROM crawl_pages WHERE run_id=?",
                (run_id,)).fetchone()[0],
            "pages_failed": c.execute(
                "SELECT COUNT(*) FROM crawl_pages "
                "WHERE run_id=? AND error IS NOT NULL", (run_id,)).fetchone()[0],
            "occurrences": c.execute(
                "SELECT COUNT(*) FROM occurrences WHERE run_id=?",
                (run_id,)).fetchone()[0],
            "unique_products": c.execute(
                "SELECT COUNT(DISTINCT product_id) FROM occurrences "
                "WHERE run_id=?", (run_id,)).fetchone()[0],
            "keywords": c.execute(
                "SELECT COUNT(*) FROM keyword_results WHERE run_id=?",
                (run_id,)).fetchone()[0],
            "zero_result_keywords": c.execute(
                "SELECT COUNT(*) FROM keyword_results "
                "WHERE run_id=? AND zero_result=1", (run_id,)).fetchone()[0],
            "failed_keywords": c.execute(
                "SELECT COUNT(*) FROM keyword_results "
                "WHERE run_id=? AND status='failed'", (run_id,)).fetchone()[0],
        }
