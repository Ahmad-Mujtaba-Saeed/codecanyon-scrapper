"""CSV exports (spec section 15).

Five files, deliberately not one giant CSV:

  keywords.csv           what we searched and why
  products.csv           one unique row per product
  search_occurrences.csv where each product appeared
  keyword_summary.csv    statistics per keyword
  research_runs.csv      historical snapshots, across all runs

Per-run files live in research/csv/<run_id>/ so a later run never overwrites
an earlier one -- that history is what makes the growth analysis in spec
section 16 possible. research_runs.csv is cross-run and sits at the top.
"""

import csv
import os

from . import stats


def _write(path, fieldnames, rows):
    # utf-8-sig writes a BOM. Without it Excel opens these as ANSI and mangles
    # every product title containing an en-dash or an accent, which on
    # CodeCanyon is a lot of them. Python's csv reader strips the BOM when the
    # file is read back with encoding="utf-8-sig".
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def export_keywords(conn, run_id, out_dir):
    rows = conn.execute(
        "SELECT k.keyword, k.parent_topic, k.source, k.approved, k.priority, "
        "r.total_results, r.unique_products, r.pages_crawled, r.zero_result "
        "FROM keyword_results r LEFT JOIN keywords k USING(keyword) "
        "WHERE r.run_id=? ORDER BY r.total_results DESC", (run_id,)).fetchall()

    return _write(
        os.path.join(out_dir, "keywords.csv"),
        ["keyword", "parent_topic", "source", "approved", "priority",
         "total_results", "unique_products", "pages_crawled", "zero_result"],
        [{**dict(r), "approved": "yes" if r["approved"] else "no",
          "zero_result": "yes" if r["zero_result"] else "no"} for r in rows])


def export_products(conn, run_id, out_dir):
    """Products grouped by the keyword that found them.

    Ordered keyword by keyword, and by search rank within each, so the file
    reads as "here is what this search returned, in order".

    A product matching several keywords therefore appears once per keyword.
    That is the cost of the grouping: it is a results file, not a unique
    product index. `product_id` still identifies a product across the whole
    file, and search_occurrences.csv remains the exhaustive pairing.
    """
    rows = []
    for group in stats.products_by_keyword(conn, run_id):
        for product in group["products"]:
            rows.append({**product, "keyword": group["keyword"]})

    return _write(
        os.path.join(out_dir, "products.csv"),
        ["keyword", "position", "product_id", "title", "author_name", "price",
         "sales", "rating", "review_count", "category", "subcategory",
         "software_version", "framework", "file_types", "last_updated"],
        rows)


def export_occurrences(conn, run_id, out_dir):
    """product x keyword x sort x page x position (spec section 13)."""
    rows = conn.execute(
        "SELECT o.product_id, p.title, o.keyword, o.sort, o.page, o.position, "
        "p.sales, o.scraped_at FROM occurrences o "
        "LEFT JOIN products p USING(product_id) "
        "WHERE o.run_id=? ORDER BY o.keyword, o.position", (run_id,)).fetchall()

    return _write(
        os.path.join(out_dir, "search_occurrences.csv"),
        ["product_id", "title", "keyword", "sort", "page", "position", "sales",
         "scraped_at"],
        [dict(r) for r in rows])


def export_keyword_summary(conn, run_id, out_dir):
    return _write(
        os.path.join(out_dir, "keyword_summary.csv"),
        ["keyword", "sort", "results", "unique_products", "pages_crawled",
         "zero_result", "total_sales", "top_sales", "avg_sales",
         "median_sales", "avg_price", "median_price"],
        [{**r, "zero_result": "yes" if r["zero_result"] else "no"}
         for r in stats.keyword_summary(conn, run_id)])


def export_research_runs(conn, csv_root):
    """Cross-run history: one row per run, for tracking a market over time."""
    rows = []
    for run in conn.execute(
            "SELECT * FROM research_runs ORDER BY started_at").fetchall():
        overview = stats.market_overview(conn, run["run_id"])
        rows.append({
            "run_id": run["run_id"],
            "topic": run["topic"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "keywords": conn.execute(
                "SELECT COUNT(*) FROM keyword_results WHERE run_id=?",
                (run["run_id"],)).fetchone()[0],
            "pages": conn.execute(
                "SELECT COUNT(*) FROM crawl_pages WHERE run_id=?",
                (run["run_id"],)).fetchone()[0],
            "unique_products": overview["unique_products"],
            "total_sales": overview["total_sales"],
            "median_sales": overview["median_sales"],
            "avg_price": overview["avg_price"],
        })

    return _write(
        os.path.join(csv_root, "research_runs.csv"),
        ["run_id", "topic", "started_at", "finished_at", "status", "keywords",
         "pages", "unique_products", "total_sales", "median_sales",
         "avg_price"],
        rows)


def export_all(conn, run_id, csv_root):
    out_dir = os.path.join(csv_root, run_id)
    return [
        export_keywords(conn, run_id, out_dir),
        export_products(conn, run_id, out_dir),
        export_occurrences(conn, run_id, out_dir),
        export_keyword_summary(conn, run_id, out_dir),
        export_research_runs(conn, csv_root),
    ]
