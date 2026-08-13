"""Statistics over a research run.

Everything here reads from SQLite and returns plain dicts and lists, so the
same numbers feed the CSV exports, the HTML report and the AI analysis pack
without being computed three different ways.

A note on what these numbers mean. `sales` is a lifetime total, not a rate,
so a 3000-sale product that last updated in 2019 is a different signal from a
3000-sale product updated last month. That is why age analysis sits alongside
the sales figures rather than being folded into them.
"""

import datetime
import re
import statistics


def _median(values):
    return round(statistics.median(values), 1) if values else 0


def _mean(values):
    return round(statistics.fmean(values), 1) if values else 0


def _days_since(iso_date, today=None):
    if not iso_date:
        return None
    try:
        then = datetime.date.fromisoformat(iso_date)
    except ValueError:
        return None
    return ((today or datetime.date.today()) - then).days


# --------------------------------------------------------------- selection

def products_in_run(conn, run_id):
    """Every distinct product seen during a run, with its run snapshot."""
    return conn.execute(
        "SELECT p.*, s.sales AS run_sales, s.price AS run_price "
        "FROM products p "
        "JOIN product_snapshots s ON s.product_id = p.product_id "
        "WHERE s.run_id = ? ORDER BY COALESCE(s.sales, 0) DESC",
        (run_id,),
    ).fetchall()


def latest_run_id(conn):
    """The run to show by default.

    Completed runs win over aborted or in-progress ones. An interrupted crawl
    holds a partial slice of the market, and defaulting to it would quietly
    present those partial numbers as the current picture.
    """
    row = conn.execute(
        "SELECT run_id FROM research_runs WHERE status='completed' "
        "ORDER BY started_at DESC LIMIT 1").fetchone()
    if row:
        return row["run_id"]

    row = conn.execute(
        "SELECT run_id FROM research_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None


# ----------------------------------------------------------------- summary

def keyword_summary(conn, run_id):
    """Per-keyword statistics (spec section 15).

    Zero-result keywords are included with zeroed statistics rather than
    omitted -- an empty search is a competition signal, not missing data.
    """
    rows = []
    for kr in conn.execute(
            "SELECT * FROM keyword_results WHERE run_id=? "
            "ORDER BY total_results DESC, keyword", (run_id,)):

        sales = [r["sales"] for r in conn.execute(
            "SELECT DISTINCT o.product_id, p.sales FROM occurrences o "
            "JOIN products p USING(product_id) "
            "WHERE o.run_id=? AND o.keyword=? AND o.sort=?",
            (run_id, kr["keyword"], kr["sort"])) if r["sales"] is not None]

        prices = [r["price"] for r in conn.execute(
            "SELECT DISTINCT o.product_id, p.price FROM occurrences o "
            "JOIN products p USING(product_id) "
            "WHERE o.run_id=? AND o.keyword=? AND o.sort=?",
            (run_id, kr["keyword"], kr["sort"])) if r["price"] is not None]

        rows.append({
            "keyword": kr["keyword"],
            "sort": kr["sort"],
            "results": kr["total_results"] or 0,
            "unique_products": kr["unique_products"] or 0,
            "pages_crawled": kr["pages_crawled"] or 0,
            "zero_result": bool(kr["zero_result"]),
            "total_sales": sum(sales),
            "top_sales": max(sales) if sales else 0,
            "avg_sales": _mean(sales),
            "median_sales": _median(sales),
            "avg_price": _mean(prices),
            "median_price": _median(prices),
        })
    return rows


def market_overview(conn, run_id):
    """Demand and competition headline numbers (spec section 18 A and B)."""
    products = products_in_run(conn, run_id)
    sales = [p["sales"] for p in products if p["sales"] is not None]
    prices = [p["price"] for p in products if p["price"] is not None]
    ratings = [p["rating"] for p in products if p["rating"] is not None]

    sold = [s for s in sales if s > 0]
    total_sales = sum(sales)

    # How concentrated is the market? If the top 10 products hold most of the
    # sales, the long tail is not really competing.
    top_ten = sorted(sales, reverse=True)[:10]

    return {
        "run_id": run_id,
        "unique_products": len(products),
        "total_sales": total_sales,
        "total_revenue_estimate": round(sum(
            (p["sales"] or 0) * (p["price"] or 0) for p in products), 2),
        "products_with_sales": len(sold),
        "products_without_sales": len(sales) - len(sold),
        "top_sales": max(sales) if sales else 0,
        "avg_sales": _mean(sales),
        "median_sales": _median(sales),
        "avg_sales_of_selling_products": _mean(sold),
        "top10_share_of_sales": (
            round(100 * sum(top_ten) / total_sales, 1) if total_sales else 0),
        "avg_price": _mean(prices),
        "median_price": _median(prices),
        "avg_rating": _mean(ratings),
        "rated_products": len(ratings),
        "unique_authors": len({p["author_name"] for p in products
                               if p["author_name"]}),
    }


def sales_distribution(conn, run_id):
    """How sales are spread. A market where 80% of products never sell is a
    different proposition from one where most products find buyers."""
    buckets = [(0, 0, "no sales"), (1, 9, "1-9"), (10, 49, "10-49"),
               (50, 199, "50-199"), (200, 999, "200-999"),
               (1000, None, "1000+")]
    products = products_in_run(conn, run_id)
    total = len(products)

    out = []
    for low, high, label in buckets:
        count = sum(1 for p in products
                    if p["sales"] is not None and p["sales"] >= low
                    and (high is None or p["sales"] <= high))
        out.append({
            "bucket": label,
            "products": count,
            "share": round(100 * count / total, 1) if total else 0,
        })
    return out


def feature_analysis(conn, run_id, features, today=None):
    """Which features recur, and do they sell (spec section 18 D).

    Matching is on product titles only. Titles are a marketing surface, so
    this measures which features vendors think are worth advertising -- which
    is close to, but not identical to, which features exist.
    """
    products = products_in_run(conn, run_id)
    out = []

    for feature in features:
        pattern = re.compile(feature["pattern"], re.I)
        matched = [p for p in products if p["title"] and pattern.search(p["title"])]
        sales = [p["sales"] for p in matched if p["sales"] is not None]
        sold = [s for s in sales if s > 0]
        fresh = [p for p in matched
                 if (_days_since(p["last_updated"], today) or 9999) <= 90]

        out.append({
            "feature": feature["name"],
            "products": len(matched),
            "total_sales": sum(sales),
            "top_sales": max(sales) if sales else 0,
            "avg_sales": _mean(sales),
            "median_sales": _median(sales),
            "products_with_sales": len(sold),
            "recently_updated": len(fresh),
            "avg_price": _mean([p["price"] for p in matched
                                if p["price"] is not None]),
        })

    return sorted(out, key=lambda r: (-r["products"], -r["total_sales"]))


def age_analysis(conn, run_id, cfg_analysis, today=None):
    """Product freshness versus performance (spec section 18 C).

    Only last_updated is available from search pages; there is no published
    date, so this measures maintenance activity rather than product age.
    """
    products = products_in_run(conn, run_id)
    stale_days = cfg_analysis.get("stale_days", 365)
    fresh_days = cfg_analysis.get("fresh_days", 90)
    low_sales = cfg_analysis.get("low_performer_sales", 5)

    sales = [p["sales"] for p in products if p["sales"] is not None]
    median_sales = statistics.median(sales) if sales else 0

    old_still_selling, new_getting_sales, low_performers, unmaintained = [], [], [], []

    for p in products:
        age = _days_since(p["last_updated"], today)
        sold = p["sales"] or 0
        if age is None:
            continue
        if age > stale_days and sold > median_sales:
            old_still_selling.append(p)
        if age <= fresh_days and sold > 0:
            new_getting_sales.append(p)
        if sold <= low_sales:
            low_performers.append(p)
        if age > stale_days:
            unmaintained.append(p)

    return {
        "median_sales": median_sales,
        "old_still_selling": len(old_still_selling),
        "new_getting_sales": len(new_getting_sales),
        "low_performers": len(low_performers),
        "unmaintained": len(unmaintained),
        "unmaintained_share": (
            round(100 * len(unmaintained) / len(products), 1)
            if products else 0),
        "examples": {
            "old_still_selling": [
                {"title": p["title"], "sales": p["sales"],
                 "last_updated": p["last_updated"]}
                for p in sorted(old_still_selling,
                                key=lambda x: -(x["sales"] or 0))[:5]],
            "new_getting_sales": [
                {"title": p["title"], "sales": p["sales"],
                 "last_updated": p["last_updated"]}
                for p in sorted(new_getting_sales,
                                key=lambda x: -(x["sales"] or 0))[:5]],
        },
    }


def top_products(conn, run_id, limit=20):
    return [{
        "product_id": p["product_id"],
        "title": p["title"],
        "author": p["author_name"],
        "price": p["price"],
        "sales": p["sales"],
        "rating": p["rating"],
        "reviews": p["review_count"],
        "last_updated": p["last_updated"],
        "url": p["url"],
    } for p in products_in_run(conn, run_id)[:limit]]


def author_concentration(conn, run_id, limit=10):
    """Who owns this market. A handful of authors holding most of the sales
    means the barrier is reputation, not features."""
    products = products_in_run(conn, run_id)
    totals = {}
    for p in products:
        if not p["author_name"]:
            continue
        entry = totals.setdefault(
            p["author_name"], {"author": p["author_name"], "products": 0,
                               "sales": 0})
        entry["products"] += 1
        entry["sales"] += p["sales"] or 0

    ranked = sorted(totals.values(), key=lambda r: -r["sales"])
    total_sales = sum(r["sales"] for r in ranked) or 1
    for row in ranked:
        row["share"] = round(100 * row["sales"] / total_sales, 1)
    return ranked[:limit]


def cross_keyword_products(conn, run_id, limit=20):
    """Products matching several keywords have broad market relevance
    (spec section 13)."""
    return [{
        "product_id": r["product_id"],
        "title": r["title"],
        "sales": r["sales"],
        "keywords": r["keyword_count"],
        "best_position": r["best_position"],
        "matched": r["matched"],
    } for r in conn.execute(
        "SELECT o.product_id, p.title, p.sales, "
        "COUNT(DISTINCT o.keyword) AS keyword_count, "
        "MIN(o.position) AS best_position, "
        "GROUP_CONCAT(DISTINCT o.keyword) AS matched "
        "FROM occurrences o JOIN products p USING(product_id) "
        "WHERE o.run_id=? GROUP BY o.product_id "
        "HAVING keyword_count > 1 "
        "ORDER BY keyword_count DESC, p.sales DESC LIMIT ?",
        (run_id, limit))]


def run_report(conn, run_id, cfg):
    """Everything about one run, in a single structure."""
    analysis_cfg = cfg.get("analysis", {}) or {}
    run = conn.execute("SELECT * FROM research_runs WHERE run_id=?",
                       (run_id,)).fetchone()
    return {
        "run": dict(run) if run else {"run_id": run_id},
        "overview": market_overview(conn, run_id),
        "keywords": keyword_summary(conn, run_id),
        "distribution": sales_distribution(conn, run_id),
        "features": feature_analysis(
            conn, run_id, analysis_cfg.get("features", [])),
        "age": age_analysis(conn, run_id, analysis_cfg),
        "top_products": top_products(conn, run_id),
        "authors": author_concentration(conn, run_id),
        "cross_keyword": cross_keyword_products(conn, run_id),
    }
