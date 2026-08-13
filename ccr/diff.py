"""Cross-run comparison (spec section 16).

The point of running research repeatedly is the delta: "341 products in May,
375 in August" and "product A went from 20 sales to 143" say more about a
market than either snapshot alone.

The trap this module exists to avoid: **two runs rarely search the same
keywords.** If run A searched 3 keywords and run B searched 8, a naive
comparison reports every product unique to B as "new" and every product
found only under A's dropped keywords as "gone" -- pure artefact, no market
change. So the comparison is scope-aware:

  - Products are compared only within the keywords **both** runs searched.
  - Keywords unique to either run are reported separately, as scope change.
  - When scope differs, every appearance/disappearance count is labelled as
    scope-limited so it is never mistaken for a market movement.

Where the scopes match exactly, the restriction is a no-op and the numbers
mean what they appear to mean.
"""


def _keyword_scope(conn, run_id):
    return {r["keyword"] for r in conn.execute(
        "SELECT DISTINCT keyword FROM keyword_results WHERE run_id=?",
        (run_id,))}


def _products_within(conn, run_id, keywords):
    """Snapshot values for products found under the given keywords."""
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    rows = conn.execute(
        f"SELECT DISTINCT s.product_id, s.sales, s.price, s.rating, "
        f"s.review_count, s.last_updated, p.title, p.author_name, p.url "
        f"FROM product_snapshots s "
        f"JOIN products p USING(product_id) "
        f"WHERE s.run_id=? AND s.product_id IN ("
        f"  SELECT product_id FROM occurrences WHERE run_id=? "
        f"  AND keyword IN ({placeholders}))",
        (run_id, run_id, *keywords)).fetchall()
    return {r["product_id"]: dict(r) for r in rows}


def _best_positions(conn, run_id, keywords):
    if not keywords:
        return {}
    placeholders = ",".join("?" for _ in keywords)
    return {r["product_id"]: r["best"] for r in conn.execute(
        f"SELECT product_id, MIN(position) AS best FROM occurrences "
        f"WHERE run_id=? AND keyword IN ({placeholders}) GROUP BY product_id",
        (run_id, *keywords))}


def compare_runs(conn, run_a, run_b):
    """Compare an earlier run (a) with a later one (b)."""
    meta_a = conn.execute("SELECT * FROM research_runs WHERE run_id=?",
                          (run_a,)).fetchone()
    meta_b = conn.execute("SELECT * FROM research_runs WHERE run_id=?",
                          (run_b,)).fetchone()
    if not meta_a or not meta_b:
        missing = run_a if not meta_a else run_b
        raise ValueError(f"no such run: {missing}")

    scope_a, scope_b = _keyword_scope(conn, run_a), _keyword_scope(conn, run_b)
    shared = scope_a & scope_b
    scope_matches = scope_a == scope_b

    products_a = _products_within(conn, run_a, shared)
    products_b = _products_within(conn, run_b, shared)
    positions_a = _best_positions(conn, run_a, shared)
    positions_b = _best_positions(conn, run_b, shared)

    ids_a, ids_b = set(products_a), set(products_b)

    appeared = [products_b[i] for i in ids_b - ids_a]
    disappeared = [products_a[i] for i in ids_a - ids_b]

    changed, movers = [], []
    for pid in ids_a & ids_b:
        before, after = products_a[pid], products_b[pid]
        sales_delta = (after["sales"] or 0) - (before["sales"] or 0)
        price_delta = round((after["price"] or 0) - (before["price"] or 0), 2)
        rank_before = positions_a.get(pid)
        rank_after = positions_b.get(pid)

        if sales_delta or price_delta:
            changed.append({
                "product_id": pid,
                "title": after["title"],
                "author": after["author_name"],
                "sales_before": before["sales"],
                "sales_after": after["sales"],
                "sales_delta": sales_delta,
                "price_before": before["price"],
                "price_after": after["price"],
                "price_delta": price_delta,
                "rank_before": rank_before,
                "rank_after": rank_after,
                # Positive means it moved up the results (toward rank 1).
                "rank_delta": (rank_before - rank_after
                               if rank_before and rank_after else None),
                "url": after["url"],
            })

        if rank_before and rank_after and rank_before != rank_after:
            movers.append({
                "product_id": pid, "title": after["title"],
                "rank_before": rank_before, "rank_after": rank_after,
                "rank_delta": rank_before - rank_after,
                "sales_delta": sales_delta,
            })

    changed.sort(key=lambda r: -r["sales_delta"])
    movers.sort(key=lambda r: -abs(r["rank_delta"]))
    appeared.sort(key=lambda r: -(r["sales"] or 0))
    disappeared.sort(key=lambda r: -(r["sales"] or 0))

    # Pagination coverage. Paginating a *live* result set can show the same
    # item on two consecutive pages and skip another, so a run may capture
    # fewer distinct products than the site reported results. Observed for
    # real: one run took 150 results as 148 unique products, and the two it
    # missed then looked like new arrivals in the next run. Where coverage is
    # incomplete, small appear/disappear counts are collection noise.
    coverage = []
    for keyword in sorted(shared):
        for run_id, label in ((run_a, "a"), (run_b, "b")):
            row = conn.execute(
                "SELECT total_results, unique_products FROM keyword_results "
                "WHERE run_id=? AND keyword=?", (run_id, keyword)).fetchone()
            if row and row["total_results"] and row["unique_products"]:
                missed = row["total_results"] - row["unique_products"]
                if missed > 0:
                    coverage.append({"run": run_id, "which": label,
                                     "keyword": keyword,
                                     "results": row["total_results"],
                                     "captured": row["unique_products"],
                                     "missed": missed})

    # An "appeared" product whose last-updated date predates the earlier run
    # already existed; it was either newly ranked into scope or missed by
    # that run's pagination. Either way it is not a new arrival.
    cutoff = (meta_a["started_at"] or "")[:10]
    for product in appeared:
        updated = product.get("last_updated") or ""
        product["existed_before"] = bool(updated and updated <= cutoff)

    keyword_changes = []
    for keyword in sorted(shared):
        row_a = conn.execute(
            "SELECT total_results, unique_products FROM keyword_results "
            "WHERE run_id=? AND keyword=?", (run_a, keyword)).fetchone()
        row_b = conn.execute(
            "SELECT total_results, unique_products FROM keyword_results "
            "WHERE run_id=? AND keyword=?", (run_b, keyword)).fetchone()
        if not row_a or not row_b:
            continue
        keyword_changes.append({
            "keyword": keyword,
            "results_before": row_a["total_results"],
            "results_after": row_b["total_results"],
            "results_delta": (row_b["total_results"] or 0)
                             - (row_a["total_results"] or 0),
            "products_before": row_a["unique_products"],
            "products_after": row_b["unique_products"],
        })
    keyword_changes.sort(key=lambda r: -abs(r["results_delta"]))

    total_sales_a = sum(p["sales"] or 0 for p in products_a.values())
    total_sales_b = sum(p["sales"] or 0 for p in products_b.values())

    return {
        "run_a": dict(meta_a),
        "run_b": dict(meta_b),
        "scope": {
            "matches": scope_matches,
            "shared": sorted(shared),
            "only_in_a": sorted(scope_a - scope_b),
            "only_in_b": sorted(scope_b - scope_a),
        },
        "totals": {
            "products_before": len(products_a),
            "products_after": len(products_b),
            "products_delta": len(products_b) - len(products_a),
            "sales_before": total_sales_a,
            "sales_after": total_sales_b,
            "sales_delta": total_sales_b - total_sales_a,
            "appeared": len(appeared),
            "appeared_existing": sum(1 for p in appeared
                                     if p.get("existed_before")),
            "disappeared": len(disappeared),
            "changed": len(changed),
        },
        "coverage": coverage,
        "appeared": appeared[:40],
        "disappeared": disappeared[:40],
        "changed": changed[:60],
        "movers": movers[:40],
        "keywords": keyword_changes,
    }


def format_text(result, log):
    """Print a comparison to the console."""
    scope = result["scope"]
    totals = result["totals"]

    log.rule("scope")
    log(f"{result['run_a']['run_id']} -> {result['run_b']['run_id']}")
    log(f"shared keywords: {len(scope['shared'])}")

    if not scope["matches"]:
        log("")
        log("WARNING: the two runs did not search the same keywords.")
        if scope["only_in_a"]:
            log(f"  only in {result['run_a']['run_id']}: "
                f"{', '.join(scope['only_in_a'])}")
        if scope["only_in_b"]:
            log(f"  only in {result['run_b']['run_id']}: "
                f"{', '.join(scope['only_in_b'])}")
        log("  Comparison is restricted to the shared keywords, so the counts")
        log("  below are scope-limited and are not whole-market movements.")

    if not scope["shared"]:
        log("")
        log("no keywords in common; there is nothing comparable here")
        return

    log.rule("totals (shared keywords only)")
    log(f"products   {totals['products_before']} -> {totals['products_after']}"
        f"  ({totals['products_delta']:+d})")
    log(f"sales      {totals['sales_before']:,} -> {totals['sales_after']:,}"
        f"  ({totals['sales_delta']:+,})")
    log(f"appeared {totals['appeared']}, disappeared {totals['disappeared']}, "
        f"changed {totals['changed']}")

    if result["keywords"]:
        log.rule("result counts per keyword")
        for k in result["keywords"]:
            log(f"  {k['keyword']:<24} {k['results_before']:>5} -> "
                f"{k['results_after']:<5} ({k['results_delta']:+d})")

    gainers = [c for c in result["changed"] if c["sales_delta"] > 0][:10]
    if gainers:
        log.rule("biggest sales gains")
        for c in gainers:
            log(f"  {c['sales_delta']:>+6,} sales  "
                f"{c['sales_before']:,} -> {c['sales_after']:,}  "
                f"{(c['title'] or '')[:46]}")

    if result["coverage"]:
        log.rule("pagination coverage")
        log("These keywords returned more results than the run captured as")
        log("distinct products. Paginating a live result set can repeat one")
        log("item across two pages and skip another, so appear/disappear")
        log("counts of this size are collection noise, not market movement.")
        for c in result["coverage"]:
            log(f"  {c['keyword']:<24} run {c['run']}: {c['results']} results, "
                f"{c['captured']} captured ({c['missed']} missed)")

    if result["appeared"]:
        log.rule("newly seen products")
        for p in result["appeared"][:10]:
            tag = ("  [existed before; newly ranked or previously missed]"
                   if p.get("existed_before") else "")
            log(f"  {p['sales'] or 0:>6,} sales  {(p['title'] or '')[:52]}{tag}")
        existing = result["totals"]["appeared_existing"]
        if existing:
            log(f"  -> {existing} of {result['totals']['appeared']} were last "
                f"updated before the earlier run, so they are not new arrivals")

    if result["disappeared"]:
        log.rule("no longer found")
        for p in result["disappeared"][:10]:
            log(f"  {p['sales'] or 0:>6,} sales  {(p['title'] or '')[:52]}")

    if not (result["changed"] or result["appeared"] or result["disappeared"]):
        log("")
        log("no differences within the shared keyword scope")
