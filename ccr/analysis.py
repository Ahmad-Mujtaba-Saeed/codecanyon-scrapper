"""The analysis bundle (spec section 17).

What the model receives is *not* the raw HTML, not 10,000 URLs, and not the
full CSVs. It is a compact structured digest: the keyword table, the market
overview, the distribution, the feature table and the top products, plus the
explicit caveats that stop a reader misreading lifetime sales as demand.

Two outputs, always both:

  dataset.md   the digest
  prompt.md    the digest with the full instructions prepended, ready to
               paste into any chat model

and, when an API key is present, analysis.md written by gpt-4o-mini from the
same text. The bundle is the deliverable; the API call is a convenience on
top of it. That way the research outlives any one provider.
"""

import os

from . import ai, stats


def _table(headers, rows):
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if c is None else str(c)
                                     for c in row) + " |")
    return "\n".join(out) + "\n"


def build_bundle(conn, run_id, cfg):
    """Compact Markdown digest of a run."""
    report = stats.run_report(conn, run_id, cfg)
    ai_cfg = cfg.get("ai", {}) or {}
    top_n = ai_cfg.get("bundle_top_products", 60)

    run = report["run"]
    o = report["overview"]
    topic = run.get("topic") or run_id

    parts = [f"# CodeCanyon research dataset: {topic}", ""]
    parts.append(f"- Research run: `{run_id}`")
    parts.append(f"- Collected: {(run.get('started_at') or '')[:10]}")
    parts.append(f"- Keywords searched: {len(report['keywords'])}")
    parts.append(f"- Unique products found: {o['unique_products']}")
    parts.append(f"- Source: CodeCanyon search results, relevance order")
    parts.append("")

    parts.append("## Market overview\n")
    parts.append(_table(["Metric", "Value"], [
        ["Unique products", o["unique_products"]],
        ["Total lifetime sales", f"{o['total_sales']:,}"],
        ["Gross revenue estimate (sales x list price)",
         f"${o['total_revenue_estimate']:,.0f}"],
        ["Products with at least one sale", o["products_with_sales"]],
        ["Products with no sales", o["products_without_sales"]],
        ["Best-selling product", f"{o['top_sales']:,}"],
        ["Average sales", o["avg_sales"]],
        ["Median sales", o["median_sales"]],
        ["Share of sales held by top 10 products",
         f"{o['top10_share_of_sales']}%"],
        ["Average price", f"${o['avg_price']}"],
        ["Median price", f"${o['median_price']}"],
        ["Average rating", f"{o['avg_rating']} ({o['rated_products']} rated)"],
        ["Distinct authors", o["unique_authors"]],
    ]))

    parts.append("## Keywords searched\n")
    parts.append("Zero-result keywords are included deliberately: an empty "
                 "search is evidence about competition.\n")
    parts.append(_table(
        ["Keyword", "Results", "Unique products", "Total sales", "Top sales",
         "Median sales", "Median price"],
        [[k["keyword"], k["results"], k["unique_products"],
          f"{k['total_sales']:,}", k["top_sales"], k["median_sales"],
          f"${k['median_price']}"] for k in report["keywords"]]))

    parts.append("## Sales distribution\n")
    parts.append(_table(["Sales band", "Products", "Share"],
                        [[d["bucket"], d["products"], f"{d['share']}%"]
                         for d in report["distribution"]]))

    parts.append("## Features mentioned in product titles\n")
    parts.append("Counts reflect what vendors advertise, not verified "
                 "capability.\n")
    parts.append(_table(
        ["Feature", "Products", "Total sales", "Median sales", "Top sales",
         "Updated in last 90 days", "Avg price"],
        [[f["feature"], f["products"], f"{f['total_sales']:,}",
          f["median_sales"], f["top_sales"], f["recently_updated"],
          f"${f['avg_price']}"] for f in report["features"]]))

    parts.append("## Author concentration\n")
    parts.append(_table(["Author", "Products", "Total sales", "Share of sales"],
                        [[a["author"], a["products"], f"{a['sales']:,}",
                          f"{a['share']}%"] for a in report["authors"]]))

    age = report["age"]
    parts.append("## Maintenance\n")
    parts.append("Search pages expose last-updated but not a published date, "
                 "so this is maintenance activity, not product age.\n")
    parts.append(_table(["Metric", "Value"], [
        ["Not updated in over a year",
         f"{age['unmaintained']} ({age['unmaintained_share']}%)"],
        ["Stale but selling above median", age["old_still_selling"]],
        ["Updated within 90 days and selling", age["new_getting_sales"]],
        ["Five sales or fewer", age["low_performers"]],
    ]))

    parts.append("## Products matching multiple keywords\n")
    parts.append(_table(["Product", "Keywords matched", "Best rank", "Sales"],
                        [[x["title"], x["keywords"], x["best_position"],
                          f"{x['sales']:,}"]
                         for x in report["cross_keyword"][:15]]))

    products = stats.products_in_run(conn, run_id)[:top_n]
    parts.append(f"## Top {len(products)} products by sales\n")
    parts.append(_table(
        ["Product", "Author", "Price", "Sales", "Rating", "Reviews",
         "Last updated"],
        [[p["title"], p["author_name"], f"${p['price']}", f"{p['sales']:,}",
          p["rating"] or "-", p["review_count"] or 0, p["last_updated"] or "-"]
         for p in products]))

    parts.append("## How this data was collected\n")
    parts.append(
        "- CodeCanyon search results only, in relevance order. Sorted result "
        "pages are disallowed by the site's robots.txt and were not fetched; "
        "sales, rating and last-updated appear on relevance pages anyway.\n"
        "- Result counts are what the site returned on the collection date. "
        "Search relevance is Envato's judgement, not a complete inventory of "
        "the market.\n"
        "- Sales are lifetime totals published by CodeCanyon, not rates.\n"
        "- Revenue is sales x list price: it ignores discounts, Envato's cut "
        "and refunds, so it is an order of magnitude, not a figure.\n"
        "- Products are identified by Envato item id, so the same product "
        "found under several keywords is counted once.\n"
        "- Paginating a live result set can show one item on two consecutive "
        "pages and skip another, so the number of distinct products captured "
        "for a keyword is occasionally one or two short of the result count "
        "the site reported. Treat small differences as collection noise.\n")

    return "\n".join(parts), topic


def write_bundle(conn, run_id, cfg, out_dir=None):
    """Write dataset.md and the paste-ready prompt.md."""
    bundle_text, topic = build_bundle(conn, run_id, cfg)

    out_dir = out_dir or os.path.join(cfg.resolve("analysis"), run_id)
    os.makedirs(out_dir, exist_ok=True)

    dataset_path = os.path.join(out_dir, "dataset.md")
    with open(dataset_path, "w", encoding="utf-8") as f:
        f.write(bundle_text)

    prompt_path = os.path.join(out_dir, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(ai.ANALYSIS_SYSTEM + "\n\n---\n\n"
                + ai._analysis_user_prompt(bundle_text, topic))

    return {"dataset": dataset_path, "prompt": prompt_path,
            "text": bundle_text, "topic": topic, "dir": out_dir}


def analyse_run(conn, run_id, cfg, client=None, out_dir=None, use_ai=True):
    """Write the bundle, then the AI analysis if it is wanted and possible.

    Missing credentials are not an error: the bundle is the deliverable and
    the API call is a convenience, so this returns normally with
    `analysis: None` and a reason the caller can show.
    """
    bundle = write_bundle(conn, run_id, cfg, out_dir)
    ai_cfg = cfg.get("ai", {}) or {}

    if not use_ai:
        bundle["analysis"] = None
        bundle["reason"] = "AI step skipped (--bundle-only)"
        return bundle

    if client is None:
        if not ai.available(ai_cfg):
            bundle["analysis"] = None
            bundle["reason"] = (
                f"{ai_cfg.get('api_key_env', 'OPENAI_API_KEY')} is not set; "
                f"wrote the paste-ready bundle instead")
            return bundle
        client = ai.OpenAIClient(ai_cfg)

    markdown = ai.analyse(client, bundle["text"], bundle["topic"])
    path = os.path.join(bundle["dir"], "analysis.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {bundle['topic']} - market analysis\n\n"
                f"Generated by {ai_cfg.get('model', 'gpt-4o-mini')} from run "
                f"`{run_id}`.\n\n---\n\n{markdown}\n")

    bundle["analysis"] = path
    return bundle
