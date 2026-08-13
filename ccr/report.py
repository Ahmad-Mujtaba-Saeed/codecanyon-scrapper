"""Standalone HTML report for one research run.

Self-contained: the stylesheet is inlined so the file can be moved, mailed
or archived and still render. No external requests, no scripts.

Charts are single-series horizontal bars, one color, with the value beside
the bar rather than inside it -- an in-bar label on a short bar either
overflows or gets clipped, and neither is acceptable. Every chart is
accompanied by the same numbers in a table, so nothing is reachable only
through the visual.
"""

import datetime
import html
import os

from . import stats

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")


def e(value):
    return html.escape("" if value is None else str(value))


def num(value, dash="-"):
    if value is None:
        return dash
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, (int, float)) else e(value)


def money(value):
    return "-" if value is None else f"${value:,.0f}"


def bars(rows, max_value=None):
    """rows: [(name, value, display)] -> single-series horizontal bar chart."""
    if not rows:
        return '<p class="muted">No data.</p>'
    top = max_value or max((r[1] for r in rows), default=0) or 1

    out = ['<div class="bars">']
    for name, value, display in rows:
        width = max(0.0, min(100.0, 100.0 * (value or 0) / top))
        out.append(
            f'<div class="bar-row"><div class="name" title="{e(name)}">'
            f'{e(name)}</div>'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="width:{width:.1f}%"></div></div>'
            f'<div class="val">{display}</div></div>')
    out.append("</div>")
    return "".join(out)


def table(headers, rows, numeric=()):
    """headers: [str]; rows: [[cell]]; numeric: indexes right-aligned."""
    if not rows:
        return '<p class="muted">No data.</p>'
    head = "".join(
        f'<th class="{"num" if i in numeric else ""}">{e(h)}</th>'
        for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if i in numeric else ""}">{cell}</td>'
            for i, cell in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _stylesheet():
    with open(os.path.join(WEB_DIR, "style.css"), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ sections

def _overview(report):
    o = report["overview"]
    run = report["run"]
    tiles = [
        ("Total sales", num(o["total_sales"]),
         "lifetime, across all products found"),
        ("Revenue estimate", money(o["total_revenue_estimate"]),
         "sales x list price, gross"),
        ("Median sales", num(o["median_sales"]),
         f"average {num(o['avg_sales'])}, skewed by the top"),
        ("Top seller", num(o["top_sales"]), "single best-selling product"),
        ("Top 10 share", f"{o['top10_share_of_sales']}%",
         "of all sales in this market"),
        ("Never sold", num(o["products_without_sales"]),
         f"of {num(o['unique_products'])} products"),
        ("Median price", money(o["median_price"]),
         f"average {money(o['avg_price'])}"),
        ("Authors", num(o["unique_authors"]), "distinct sellers"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{e(label)}</div>'
        f'<div class="value">{value}</div>'
        f'<div class="note">{e(note)}</div></div>'
        for label, value, note in tiles)

    return f"""
<section>
  <div class="card hero">
    <div class="label">Unique products found</div>
    <div class="value">{num(o['unique_products'])}</div>
    <div class="label">across {len(report['keywords'])} keywords
      &middot; {e(run.get('topic') or 'no topic')}</div>
  </div>
  <div class="tiles">{tile_html}</div>
</section>"""


def _distribution(report):
    rows = [(r["bucket"], r["products"],
             f'{num(r["products"])} <small>{r["share"]}%</small>')
            for r in report["distribution"]]
    return f"""
<section>
  <h2>Sales distribution</h2>
  <p class="sub">How sales are spread across products. A market where most
    products never sell is a different proposition from one where most find
    buyers.</p>
  <div class="card">{bars(rows)}</div>
</section>"""


def _keywords(report):
    rows = []
    for k in report["keywords"]:
        badge = ('<span class="badge zero">zero results</span>'
                 if k["zero_result"] else "")
        rows.append([
            f'<span class="strong">{e(k["keyword"])}</span> {badge}',
            num(k["results"]), num(k["unique_products"]),
            num(k["total_sales"]), num(k["top_sales"]),
            num(k["median_sales"]), money(k["median_price"]),
        ])
    return f"""
<section>
  <h2>Keywords</h2>
  <p class="sub">What was searched and what came back. Keywords returning
    nothing are kept deliberately -- an empty search says something about
    competition, and dropping it would erase that.</p>
  <div class="card">{table(
      ["Keyword", "Results", "Unique", "Total sales", "Top", "Median",
       "Median price"], rows, numeric={1, 2, 3, 4, 5, 6})}</div>
</section>"""


def _features(report):
    present = [f for f in report["features"] if f["products"]]
    absent = [f["feature"] for f in report["features"] if not f["products"]]

    chart = bars([(f["feature"], f["products"], num(f["products"]))
                  for f in present[:14]])

    rows = [[
        f'<span class="strong">{e(f["feature"])}</span>',
        num(f["products"]), num(f["total_sales"]), num(f["median_sales"]),
        num(f["top_sales"]), num(f["recently_updated"]), money(f["avg_price"]),
    ] for f in present]

    gap = ""
    if absent:
        gap = (f'<p class="sub" style="margin-top:14px">No product title '
               f'mentions: <b>{e(", ".join(absent))}</b>. Absence is a '
               f'candidate gap, but only becomes an opportunity when demand '
               f'exists elsewhere in this data.</p>')

    return f"""
<section>
  <h2>Features by product count</h2>
  <p class="sub">Matched against product titles, so this measures what
    vendors think is worth advertising -- close to, but not the same as,
    what the products actually do.</p>
  <div class="card">{chart}</div>
  <div class="card" style="margin-top:12px">{table(
      ["Feature", "Products", "Total sales", "Median", "Top", "Updated <90d",
       "Avg price"], rows, numeric={1, 2, 3, 4, 5, 6})}{gap}</div>
</section>"""


def _authors(report):
    rows = [(a["author"], a["share"],
             f'{num(a["sales"])} <small>{a["share"]}%</small>')
            for a in report["authors"]]
    leader = report["authors"][0] if report["authors"] else None
    lead_note = ""
    if leader and leader["share"] >= 25:
        lead_note = (f' <b>{e(leader["author"])}</b> alone holds '
                     f'{leader["share"]}% of sales, which makes reputation, '
                     f'not features, the entry barrier.')
    return f"""
<section>
  <h2>Author concentration</h2>
  <p class="sub">Share of total sales by seller.{lead_note}</p>
  <div class="card">{bars(rows, max_value=100)}</div>
</section>"""


def _maintenance(report):
    age = report["age"]
    tiles = [
        ("Unmaintained", num(age["unmaintained"]),
         f"{age['unmaintained_share']}% not updated in over a year"),
        ("Old but still selling", num(age["old_still_selling"]),
         "stale yet above median sales"),
        ("Fresh and selling", num(age["new_getting_sales"]),
         "updated in 90 days, with sales"),
        ("Low performers", num(age["low_performers"]), "5 sales or fewer"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{e(label)}</div>'
        f'<div class="value">{value}</div>'
        f'<div class="note">{e(note)}</div></div>'
        for label, value, note in tiles)

    examples = age["examples"]["old_still_selling"]
    rows = [[e(x["title"]), num(x["sales"]), e(x["last_updated"])]
            for x in examples]

    return f"""
<section>
  <h2>Maintenance</h2>
  <p class="sub">Search pages expose <b>last updated</b> but not a published
    date, so this measures maintenance activity rather than product age.
    Stale products that still outsell the median are the clearest sign of an
    incumbent coasting.</p>
  <div class="tiles">{tile_html}</div>
  <div class="card" style="margin-top:12px">
    <p class="sub">Stale but outselling the median</p>
    {table(["Product", "Sales", "Last updated"], rows, numeric={1})}
  </div>
</section>"""


def _top_products(report):
    rows = [[
        f'<a href="{e(p["url"])}">{e(p["title"])}</a>',
        e(p["author"]), money(p["price"]), num(p["sales"]),
        f'{p["rating"] or "-"} <small class="muted">({p["reviews"] or 0})</small>',
        e(p["last_updated"]),
    ] for p in report["top_products"]]
    return f"""
<section>
  <h2>Top products</h2>
  <div class="card">{table(
      ["Product", "Author", "Price", "Sales", "Rating", "Updated"], rows,
      numeric={2, 3, 4})}</div>
</section>"""


def _cross_keyword(report):
    rows = [[
        e(x["title"]), num(x["keywords"]), f'#{x["best_position"]}',
        num(x["sales"]), f'<span class="muted">{e(x["matched"])}</span>',
    ] for x in report["cross_keyword"]]
    return f"""
<section>
  <h2>Products matching several keywords</h2>
  <p class="sub">Breadth of match is its own signal: a product surfacing
    under many different searches occupies more of the market's mental
    space than its sales alone suggest.</p>
  <div class="card">{table(
      ["Product", "Keywords", "Best rank", "Sales", "Matched"], rows,
      numeric={1, 2, 3})}</div>
</section>"""


# --------------------------------------------------------------------- build

def build_html(report, generated_at=None):
    run = report["run"]
    generated = generated_at or datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M")
    title = f"{run.get('topic') or 'CodeCanyon'} market research"

    return f"""<!doctype html>
<html lang="en" class="viz-root">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} - {e(run.get('run_id'))}</title>
<style>{_stylesheet()}</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header class="page">
    <div>
      <h1>{e(title)}</h1>
      <div class="meta">CodeCanyon search data &middot; collected
        <b>{e((run.get('started_at') or '')[:10])}</b></div>
    </div>
    <div class="meta">
      run <b>{e(run.get('run_id'))}</b> &middot;
      status <b>{e(run.get('status'))}</b> &middot;
      report generated {e(generated)}
    </div>
  </header>

  {_overview(report)}
  {_distribution(report)}
  {_keywords(report)}
  {_features(report)}
  {_maintenance(report)}
  {_authors(report)}
  {_cross_keyword(report)}
  {_top_products(report)}

  <p class="footnote">
    <b>How to read this.</b> Sales figures are lifetime totals published by
    CodeCanyon, not rates, so an old product's total reflects years of
    accumulation rather than current demand. Revenue is sales x list price
    and ignores discounts, Envato's cut and refunds, so treat it as an order
    of magnitude. Feature counts come from product titles only. Result counts
    are what the site's search returned on the day of the run, and search
    relevance is Envato's judgement, not a complete inventory of the market.
    Paginating a live result set can show one item on two consecutive pages
    and skip another, so the number of products captured for a keyword is
    occasionally one or two short of the result count shown.
  </p>
</div>
</body>
</html>"""


def write_report(conn, run_id, cfg, out_dir=None):
    report = stats.run_report(conn, run_id, cfg)
    out_dir = out_dir or cfg.resolve("reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{run_id}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(report))
    return path
