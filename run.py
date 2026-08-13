#!/usr/bin/env python
"""CodeCanyon market research collector - command line entry point.

  python run.py smoke  --keyword "perfex integration"
  python run.py scrape --topic "Perfex CRM"
  python run.py scrape --keyword "perfex mcp" --keyword "perfex api"
  python run.py scrape --resume 20260812T103000
  python run.py runs
"""

import argparse
import os
import sys
import webbrowser

from ccr import ai
from ccr import analysis
from ccr import diff
from ccr import exporters
from ccr import keywords as keyword_file
from ccr import parser as page_parser
from ccr import report
from ccr import stats
from ccr.config import Config
from ccr.http_client import HttpClient, AbortRun
from ccr.log import Logger
from ccr.pipeline import Crawler, new_run_id
from ccr.store import Store
from ccr.throttle import Throttle
from ccr.urls import search_url


def build(cfg, log, quiet_throttle=False):
    store = Store(cfg.resolve("db"))
    client = HttpClient(cfg, etag_path=cfg.resolve("db") + ".etags", log=log)
    throttle = Throttle(cfg["throttle"], log=None if quiet_throttle else log)
    return store, client, throttle


# ------------------------------------------------------------------- smoke

def cmd_smoke(args, cfg, log):
    """Fetch and parse exactly one page. No database writes."""
    store, client, throttle = build(cfg, log)
    url = search_url(cfg.base_url, args.keyword, args.sort, args.page)

    from ccr.robots import fetch_rules
    rules = fetch_rules(client, cfg.base_url)
    log(f"robots.txt allows {url}: {rules.allowed(url)}")
    if cfg.respect_robots and not rules.allowed(url):
        log(f"blocked by rule {rules.reason(url)}; stopping")
        return 1

    log(f"fetching {url}")
    status, body, headers = client.get(url, throttle=throttle, use_etag=False)
    log(f"HTTP {status}, {len(body)} bytes, cf-cache={headers.get('cf-cache-status')}")
    if status != 200:
        return 1

    result = page_parser.parse_search_page(
        body, args.keyword, args.sort, args.page, url,
        items_per_page=cfg["items_per_page_expected"])

    log(f"total_results={result.total_results} has_next={result.has_next} "
        f"parsed={result.cards_parsed}/{result.cards_seen}")
    log("")
    for product, occurrence in list(zip(result.products, result.occurrences))[:5]:
        log(f"  #{occurrence.position:<3} {product.product_id:>9}  "
            f"{(product.title or '')[:52]:<52} "
            f"${product.price:<7} {product.sales:>6} sales  "
            f"{product.rating or '-'}/{product.review_count or 0}  "
            f"{product.author_name}")
    store.close()
    return 0


# ------------------------------------------------------------------ scrape

def cmd_scrape(args, cfg, log):
    store, client, throttle = build(cfg, log)

    if args.keyword:
        rows = [{"keyword": k, "parent_topic": args.topic, "source": "cli",
                 "approved": True, "priority": None} for k in args.keyword]
    else:
        rows = keyword_file.load(cfg.resolve("keywords"))

    if not rows:
        log("no approved keywords to crawl")
        return 1

    for row in rows:
        store.upsert_keyword(row["keyword"], row["parent_topic"],
                             row["source"], row["approved"], row["priority"])

    words = [r["keyword"] for r in rows]
    topic = args.topic or (rows[0]["parent_topic"] if rows else None)

    log.rule("plan")
    log(f"keywords ({len(words)}): {', '.join(words)}")
    log(f"sorts: {', '.join(cfg.sorts)}")
    log(f"max pages per keyword: {args.max_pages or cfg.max_pages_per_keyword}")
    log(f"respect robots.txt: {cfg.respect_robots}")
    if args.dry_run:
        for word in words:
            for sort in cfg.sorts:
                log(f"  would fetch {search_url(cfg.base_url, word, sort, 1)}")
        return 0
    log.rule()

    crawler = Crawler(cfg, store, client, throttle, log,
                      run_id=args.resume or new_run_id(),
                      max_pages=args.max_pages)

    try:
        status = crawler.crawl(words, topic=topic)
    except AbortRun as e:
        log(f"ABORTED: {e}")
        status = "aborted"

    summary = store.run_summary(crawler.run_id)
    log.rule("summary")
    log(f"run {crawler.run_id}: {status}")
    log(f"pages fetched {crawler.pages_fetched}, skipped {crawler.pages_skipped}, "
        f"failed {crawler.pages_failed}")
    log(f"unique products {summary['unique_products']}, "
        f"occurrences {summary['occurrences']}")
    log(f"keywords {summary['keywords']}, "
        f"zero-result {summary['zero_result_keywords']}")
    log(f"time spent waiting: {throttle.slept_total / 60:.1f} min")
    log(f"database: {cfg.resolve('db')}")
    store.close()
    return 0 if status == "completed" else 2


# ------------------------------------------------------------------ export

def _resolve_run(store, requested, log):
    run_id = requested or stats.latest_run_id(store.conn)
    if not run_id:
        log("no runs in the database yet; run a scrape first")
    return run_id


def cmd_export(args, cfg, log):
    store = Store(cfg.resolve("db"))
    run_id = _resolve_run(store, args.run, log)
    if not run_id:
        return 1

    paths = exporters.export_all(store.conn, run_id, cfg.resolve("csv"))
    log(f"exported run {run_id}:")
    for path in paths:
        with open(path, encoding="utf-8") as f:
            lines = sum(1 for _ in f) - 1
        log(f"  {os.path.relpath(path, os.path.dirname(cfg.resolve('csv')))}"
            f"  ({lines} rows)")
    store.close()
    return 0


# ------------------------------------------------------------------- stats

def cmd_stats(args, cfg, log):
    store = Store(cfg.resolve("db"))
    run_id = _resolve_run(store, args.run, log)
    if not run_id:
        return 1

    report = stats.run_report(store.conn, run_id, cfg)
    o = report["overview"]

    log.rule(f"market overview: {report['run'].get('topic') or run_id}")
    log(f"unique products     {o['unique_products']}")
    log(f"total sales         {o['total_sales']:,}")
    log(f"revenue estimate    ${o['total_revenue_estimate']:,.0f}")
    log(f"products with sales {o['products_with_sales']} "
        f"(never sold: {o['products_without_sales']})")
    log(f"top / avg / median  {o['top_sales']:,} / {o['avg_sales']} / "
        f"{o['median_sales']}")
    log(f"top 10 hold         {o['top10_share_of_sales']}% of all sales")
    log(f"price avg / median  ${o['avg_price']} / ${o['median_price']}")
    log(f"authors             {o['unique_authors']}")

    log.rule("sales distribution")
    for row in report["distribution"]:
        bar = "#" * int(row["share"] / 2)
        log(f"  {row['bucket']:<10} {row['products']:>4}  {row['share']:>5}% {bar}")

    log.rule("keywords")
    for row in report["keywords"]:
        flag = "  <- ZERO RESULTS" if row["zero_result"] else ""
        log(f"  {row['keyword']:<24} results={row['results']:<5} "
            f"unique={row['unique_products']:<4} top={row['top_sales']:<6} "
            f"median={row['median_sales']:<7}{flag}")

    log.rule("features that recur in titles")
    for row in report["features"][:12]:
        if not row["products"]:
            continue
        log(f"  {row['feature']:<16} {row['products']:>3} products  "
            f"{row['total_sales']:>7,} sales  median {row['median_sales']:>6}  "
            f"fresh {row['recently_updated']}")

    absent = [r["feature"] for r in report["features"] if not r["products"]]
    if absent:
        log(f"  no products mention: {', '.join(absent)}")

    log.rule("maintenance")
    age = report["age"]
    log(f"unmaintained (>1yr) {age['unmaintained']} "
        f"({age['unmaintained_share']}%)")
    log(f"old but still selling above median  {age['old_still_selling']}")
    log(f"updated recently and selling        {age['new_getting_sales']}")
    log(f"low performers (<=5 sales)          {age['low_performers']}")

    log.rule("top products")
    for p in report["top_products"][:10]:
        log(f"  {p['sales']:>6,} sales  ${p['price'] or 0:<7} "
            f"{(p['title'] or '')[:50]:<50} {p['author']}")

    log.rule("author concentration")
    for a in report["authors"][:5]:
        log(f"  {a['sales']:>7,} sales ({a['share']:>5}%)  "
            f"{a['products']:>2} products  {a['author']}")

    store.close()
    return 0


# ---------------------------------------------------------------- keywords

def cmd_keywords(args, cfg, log):
    path = cfg.resolve("keywords")

    if args.generate:
        topic = args.topic
        if not topic:
            log("--generate needs --topic")
            return 1

        ai_cfg = cfg.get("ai", {}) or {}
        if not ai.available(ai_cfg):
            log(f"{ai_cfg.get('api_key_env', 'OPENAI_API_KEY')} is not set.")
            log("Set it and retry, or add keywords to keywords.csv by hand:")
            log(f"  {path}")
            return 1

        client = ai.OpenAIClient(ai_cfg)
        try:
            existing = [r["keyword"] for r in
                        keyword_file.load(path, include_unapproved=True)]
        except FileNotFoundError:
            existing = []

        count = args.count or ai_cfg.get("keyword_count", 14)
        log(f"asking {ai_cfg.get('model')} for {count} keywords for {topic!r}…")
        try:
            generated = ai.generate_keywords(client, topic, existing, count)
        except (ai.AIError, ai.AIUnavailable) as e:
            log(f"keyword generation failed: {e}")
            return 1

        added, skipped = keyword_file.merge(path, generated)
        log(f"added {len(added)} keywords (unapproved), "
            f"{len(skipped)} already present")
        for row in added:
            log(f"  [{row.get('band', '-'):<11}] {row['keyword']:<32} "
                f"{row.get('rationale', '')}")
        log("")
        log("Nothing will be crawled until you approve them:")
        log("  python run.py keywords --approve-all")
        log(f"or edit the approved column in {path}")
        return 0

    if args.approve_all or args.approve:
        changed = keyword_file.set_approval(path, True, args.approve or None)
        log(f"approved {len(changed)} keywords"
            + (f": {', '.join(changed)}" if changed else ""))
        return 0

    if args.reject:
        changed = keyword_file.set_approval(path, False, args.reject)
        log(f"unapproved {len(changed)} keywords"
            + (f": {', '.join(changed)}" if changed else ""))
        return 0

    try:
        rows = keyword_file.load(path, include_unapproved=True)
    except FileNotFoundError:
        log(f"no keyword file at {path}")
        return 1

    approved = sum(1 for r in rows if r["approved"])
    log(f"{len(rows)} keywords, {approved} approved")
    for row in rows:
        mark = "x" if row["approved"] else " "
        log(f"  [{mark}] {row['keyword']:<34} {row['source']:<8} "
            f"{row['priority'] or ''}")
    return 0


# ----------------------------------------------------------------- analyse

def cmd_analyze(args, cfg, log):
    store = Store(cfg.resolve("db"))
    run_id = _resolve_run(store, args.run, log)
    if not run_id:
        return 1

    ai_cfg = cfg.get("ai", {}) or {}

    if not args.bundle_only and ai.available(ai_cfg):
        log(f"building bundle and asking {ai_cfg.get('model')} to analyse…")
    else:
        log("building the paste-ready bundle…")

    try:
        # analyse_run decides whether the key is present, so the reason it
        # reports is the real one rather than whatever we guessed here.
        result = analysis.analyse_run(store.conn, run_id, cfg,
                                      use_ai=not args.bundle_only)
    except (ai.AIError, ai.AIUnavailable) as e:
        log(f"AI analysis failed: {e}")
        log("the bundle was still written; you can paste it into any model")
        store.close()
        return 1

    log(f"dataset: {result['dataset']}")
    log(f"prompt:  {result['prompt']}")
    if result.get("analysis"):
        log(f"analysis: {result['analysis']}")
    else:
        log("")
        log(result.get("reason") or "AI step skipped")
        log("Paste prompt.md into any chat model to get the same analysis.")
    store.close()
    return 0


# --------------------------------------------------------------------- diff

def cmd_diff(args, cfg, log):
    store = Store(cfg.resolve("db"))
    runs = [r["run_id"] for r in store.conn.execute(
        "SELECT run_id FROM research_runs ORDER BY started_at").fetchall()]

    if len(runs) < 2 and not (args.from_run and args.to_run):
        log(f"need two runs to compare; found {len(runs)}")
        store.close()
        return 1

    run_a = args.from_run or runs[-2]
    run_b = args.to_run or runs[-1]

    try:
        result = diff.compare_runs(store.conn, run_a, run_b)
    except ValueError as e:
        log(str(e))
        store.close()
        return 1

    diff.format_text(result, log)
    store.close()
    return 0


# ------------------------------------------------------------------ report

def cmd_report(args, cfg, log):
    store = Store(cfg.resolve("db"))
    run_id = _resolve_run(store, args.run, log)
    if not run_id:
        return 1

    path = report.write_report(store.conn, run_id, cfg)
    log(f"wrote {path}")
    if args.open:
        webbrowser.open("file:///" + path.replace("\\", "/"))
    store.close()
    return 0


# -------------------------------------------------------------------- runs

def cmd_runs(args, cfg, log):
    store = Store(cfg.resolve("db"))
    rows = store.conn.execute(
        "SELECT * FROM research_runs ORDER BY started_at DESC LIMIT 20"
    ).fetchall()
    if not rows:
        log("no runs recorded yet")
        return 0
    for row in rows:
        summary = store.run_summary(row["run_id"])
        log(f"{row['run_id']}  {row['status']:<10} "
            f"{row['topic'] or '-':<20} "
            f"pages={summary['pages']:<4} products={summary['unique_products']:<5} "
            f"started {row['started_at']}")
    store.close()
    return 0


# -------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--quiet", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser("smoke", help="fetch and parse one page, no writes")
    smoke.add_argument("--keyword", required=True)
    smoke.add_argument("--sort", default="relevance")
    smoke.add_argument("--page", type=int, default=1)
    smoke.set_defaults(func=cmd_smoke)

    scrape = sub.add_parser("scrape", help="run a full research crawl")
    scrape.add_argument("--keyword", action="append",
                        help="crawl this keyword instead of keywords.csv "
                             "(repeatable)")
    scrape.add_argument("--topic", default=None)
    scrape.add_argument("--max-pages", type=int, default=None)
    scrape.add_argument("--resume", default=None, metavar="RUN_ID")
    scrape.add_argument("--dry-run", action="store_true")
    scrape.set_defaults(func=cmd_scrape)

    export = sub.add_parser("export", help="write the five research CSVs")
    export.add_argument("--run", default=None, metavar="RUN_ID")
    export.set_defaults(func=cmd_export)

    stats_cmd = sub.add_parser("stats", help="print market statistics")
    stats_cmd.add_argument("--run", default=None, metavar="RUN_ID")
    stats_cmd.set_defaults(func=cmd_stats)

    kw = sub.add_parser("keywords",
                        help="list, generate or approve research keywords")
    kw.add_argument("--generate", action="store_true",
                    help="ask the model for keywords (needs an API key)")
    kw.add_argument("--topic", default=None)
    kw.add_argument("--count", type=int, default=None)
    kw.add_argument("--approve", action="append", metavar="KEYWORD")
    kw.add_argument("--approve-all", action="store_true")
    kw.add_argument("--reject", action="append", metavar="KEYWORD")
    kw.set_defaults(func=cmd_keywords)

    analyze = sub.add_parser("analyze",
                             help="build the AI analysis bundle for a run")
    analyze.add_argument("--run", default=None, metavar="RUN_ID")
    analyze.add_argument("--bundle-only", action="store_true",
                         help="skip the API call even if a key is set")
    analyze.set_defaults(func=cmd_analyze)

    diff_cmd = sub.add_parser("diff", help="compare two research runs")
    diff_cmd.add_argument("--from", dest="from_run", default=None,
                          metavar="RUN_ID")
    diff_cmd.add_argument("--to", dest="to_run", default=None,
                          metavar="RUN_ID")
    diff_cmd.set_defaults(func=cmd_diff)

    report_cmd = sub.add_parser("report", help="write a standalone HTML report")
    report_cmd.add_argument("--run", default=None, metavar="RUN_ID")
    report_cmd.add_argument("--open", action="store_true",
                            help="open the report in a browser when done")
    report_cmd.set_defaults(func=cmd_report)

    runs = sub.add_parser("runs", help="list recorded research runs")
    runs.set_defaults(func=cmd_runs)

    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    log = Logger(quiet=args.quiet)
    return args.func(args, cfg, log)


if __name__ == "__main__":
    sys.exit(main())
