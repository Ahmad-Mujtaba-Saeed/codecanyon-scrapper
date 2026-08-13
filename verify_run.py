#!/usr/bin/env python
"""Reconciliation checks for a completed run.

Confirms the database agrees with itself and with the raw archive, rather
than trusting the crawler's own summary line.

  python verify_run.py [RUN_ID]
"""

import os
import sys

from ccr.archive import RawArchive
from ccr.config import Config
from ccr.store import Store

cfg = Config.load()
store = Store(cfg.resolve("db"))
conn = store.conn

run_id = sys.argv[1] if len(sys.argv) > 1 else conn.execute(
    "SELECT run_id FROM research_runs ORDER BY started_at DESC LIMIT 1"
).fetchone()[0]

print(f"run: {run_id}\n")
failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  [{'ok' if ok else 'FAIL'}] {label}: {actual}"
          f"{'' if ok else f' (expected {expected})'}")
    if not ok:
        failures.append(label)


print("pages crawled")
for row in conn.execute(
        "SELECT keyword, page, item_count, total_results, has_next, parse_ratio "
        "FROM crawl_pages WHERE run_id=? ORDER BY keyword, page", (run_id,)):
    print(f"  {row['keyword']:<20} page {row['page']}  "
          f"items={row['item_count']:<3} total={row['total_results']:<4} "
          f"next={bool(row['has_next'])}  parse_ratio={row['parse_ratio']:.2f}")

print("\nreconciliation")
page_items = conn.execute(
    "SELECT COALESCE(SUM(item_count),0) FROM crawl_pages WHERE run_id=?",
    (run_id,)).fetchone()[0]
occurrences = conn.execute(
    "SELECT COUNT(*) FROM occurrences WHERE run_id=?", (run_id,)).fetchone()[0]
check("occurrences equal sum of per-page item counts", occurrences, page_items)

unique_products = conn.execute(
    "SELECT COUNT(DISTINCT product_id) FROM occurrences WHERE run_id=?",
    (run_id,)).fetchone()[0]
snapshots = conn.execute(
    "SELECT COUNT(*) FROM product_snapshots WHERE run_id=?",
    (run_id,)).fetchone()[0]
check("one snapshot per unique product", snapshots, unique_products)

orphans = conn.execute(
    "SELECT COUNT(*) FROM occurrences o LEFT JOIN products p "
    "USING(product_id) WHERE o.run_id=? AND p.product_id IS NULL",
    (run_id,)).fetchone()[0]
check("no occurrences without a product row", orphans, 0)

null_ids = conn.execute(
    "SELECT COUNT(*) FROM products WHERE product_id IS NULL OR title IS NULL "
    "OR url IS NULL").fetchone()[0]
check("no products missing identity fields", null_ids, 0)

null_sales = conn.execute(
    "SELECT COUNT(*) FROM products WHERE sales IS NULL").fetchone()[0]
check("no NULL sales (absent element means zero)", null_sales, 0)

# Every page total_results should agree within a keyword.
inconsistent = conn.execute(
    "SELECT COUNT(*) FROM (SELECT keyword FROM crawl_pages WHERE run_id=? "
    "GROUP BY keyword HAVING COUNT(DISTINCT total_results) > 1)",
    (run_id,)).fetchone()[0]
check("total_results consistent across pages of a keyword", inconsistent, 0)

# Last page of each keyword must be the one with has_next = 0.
bad_pagination = conn.execute(
    "SELECT COUNT(*) FROM crawl_pages c WHERE c.run_id=? AND c.has_next=0 "
    "AND EXISTS (SELECT 1 FROM crawl_pages c2 WHERE c2.run_id=c.run_id "
    "AND c2.keyword=c.keyword AND c2.page > c.page)", (run_id,)).fetchone()[0]
check("nothing fetched after a page said it was the last", bad_pagination, 0)

missing_raw = 0
for row in conn.execute(
        "SELECT raw_path FROM crawl_pages WHERE run_id=? AND error IS NULL",
        (run_id,)):
    if not RawArchive.exists(row["raw_path"]):
        missing_raw += 1
check("every page has its archived HTML on disk", missing_raw, 0)

expected_pages = conn.execute(
    "SELECT COUNT(*) FROM crawl_pages WHERE run_id=? AND error IS NULL",
    (run_id,)).fetchone()[0]

# Archives are laid out as raw/<date>/<run_id>/..., so count only this run's
# directory. Walking all of raw/ would total every run ever collected.
archived = 0
for date_dir in os.listdir(cfg.resolve("raw")):
    run_dir = os.path.join(cfg.resolve("raw"), date_dir, run_id)
    if os.path.isdir(run_dir):
        archived += sum(len(files) for _, _, files in os.walk(run_dir))
check("archive file count matches page count", archived, expected_pages)

print("\ncross-keyword overlap (spec section 13)")
for row in conn.execute(
        "SELECT product_id, COUNT(DISTINCT keyword) k, "
        "MIN(position) best FROM occurrences WHERE run_id=? "
        "GROUP BY product_id HAVING k > 1 ORDER BY k DESC, best LIMIT 8",
        (run_id,)):
    p = conn.execute("SELECT title, sales FROM products WHERE product_id=?",
                     (row["product_id"],)).fetchone()
    print(f"  {row['product_id']:>9}  {row['k']} keywords  best rank "
          f"#{row['best']:<3} {p['sales']:>5} sales  {p['title'][:46]}")

multi = conn.execute(
    "SELECT COUNT(*) FROM (SELECT product_id FROM occurrences WHERE run_id=? "
    "GROUP BY product_id HAVING COUNT(DISTINCT keyword) > 1)",
    (run_id,)).fetchone()[0]
print(f"  -> {multi} products matched more than one keyword "
      f"({occurrences - unique_products} duplicate sightings collapsed)")

print("\nmarket snapshot (spec section 18)")
for row in conn.execute(
        "SELECT k.keyword, k.total_results, k.unique_products, "
        "MAX(p.sales) top, ROUND(AVG(p.sales),1) avg "
        "FROM keyword_results k LEFT JOIN occurrences o "
        "ON o.run_id=k.run_id AND o.keyword=k.keyword "
        "LEFT JOIN products p ON p.product_id=o.product_id "
        "WHERE k.run_id=? GROUP BY k.keyword ORDER BY k.total_results DESC",
        (run_id,)):
    print(f"  {row['keyword']:<20} results={row['total_results']:<5} "
          f"unique={row['unique_products']:<4} top_sales={row['top'] or 0:<6} "
          f"avg_sales={row['avg'] or 0}")

zero_sales = conn.execute(
    "SELECT COUNT(*) FROM products WHERE sales=0").fetchone()[0]
print(f"\n  products with zero sales: {zero_sales} of "
      f"{conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]}")

print("\n" + ("ALL CHECKS PASSED" if not failures
              else f"FAILED: {', '.join(failures)}"))
store.close()
sys.exit(1 if failures else 0)
