# CodeCanyon Market Research Collector

Collects CodeCanyon search data into a structured research dataset so market
questions ("how much competition exists for POS MCP?") can be answered from
evidence instead of impressions.

The scraper is only the data-collection layer. The value is the dataset.

**Status: complete.** Collection, storage, statistics, CSV export, the HTML
report, the local dashboard, AI keyword generation and analysis, and cross-run
diffing all work against the live site. 96 tests.

## Requirements

Python 3.13 and one dependency:

```bash
pip install beautifulsoup4
```

No Scrapy, no Selenium, no headless browser. CodeCanyon renders search results
server-side, so plain HTTP is enough.

## Usage

Fetch and parse a single page without writing anything — use this to check the
site still parses before a long run:

```bash
python run.py smoke --keyword "perfex integration"
```

Run a crawl over the keywords in `keywords.csv`:

```bash
python run.py scrape --topic "Perfex CRM"
```

Crawl specific keywords instead of the file:

```bash
python run.py scrape --keyword "perfex mcp" --keyword "perfex api"
```

Resume an interrupted run — completed pages are skipped without any network
requests:

```bash
python run.py scrape --resume 20260812T162636
```

Print market statistics for the latest run:

```bash
python run.py stats
```

Write the five CSVs, and a standalone HTML report:

```bash
python run.py export
```

```bash
python run.py report --open
```

## Running it from the browser only

One command, and everything else happens in the UI:

```bash
python serve.py
```

It opens `http://127.0.0.1:8765` (loopback only — nothing is exposed to the
network). From there:

| Tab | What you can do |
|---|---|
| **Collect** | Add a keyword, generate keywords with AI, approve or remove them, start a crawl, watch live progress, build the analysis bundle, and open or download every file the run produced |
| **Overview** | Hero count, market tiles, sales distribution, author concentration, maintenance |
| **Keywords** | Result counts and per-keyword statistics |
| **Products** | Every product, filterable and sortable by any column |
| **Features** | Which capabilities recur, and whether they sell |
| **Compare** | Diff any two runs, with scope and coverage warnings |

The header has a run picker and an **Export CSVs + report** button.

A typical session without touching the terminal again: open **Collect**, type
a keyword and press Add (or generate a set with AI and approve the ones you
want), enter a topic, press **Start run**, watch the log, then press **Export
CSVs + report** and **Build analysis bundle**. The Output files table then
gives you an `open` link for the report and `download` links for the CSVs and
the paste-ready prompt.

Two behaviours worth knowing:

- **Only one crawl runs at a time.** Two concurrent crawls would double the
  request rate against the site, which is the thing the pacing config exists
  to prevent. The Start button disables itself while a run is in progress.
- **The dashboard defaults to the newest *completed* run.** An interrupted
  crawl holds a partial slice of the market, so showing it by default would
  present partial numbers as the current picture. Aborted runs are still
  selectable and are labelled as such in the picker.

Only generated artefacts are reachable over HTTP — reports, CSVs and analysis
bundles. The SQLite database and the raw HTML archive live in the same tree
and are deliberately not served.

Generate keywords with `gpt-4o-mini`, then approve them. **Nothing is crawled
until a human approves it** — generated keywords always land unapproved:

```bash
python run.py keywords --generate --topic "Ultimate POS"
```

```bash
python run.py keywords --approve-all
```

Build the AI analysis bundle, and the analysis itself if a key is present:

```bash
python run.py analyze
```

Compare two runs (defaults to the two most recent):

```bash
python run.py diff
```

List past runs, check a run's integrity, run the tests:

```bash
python run.py runs
```

```bash
python verify_run.py
```

```bash
python -m unittest discover -s tests
```

## How it behaves toward the site

Pacing is designed to be *irregular*, not merely slow — a request every exactly
5.0 seconds for an hour is a stronger bot signal than a faster but human-shaped
pattern. Four layers, all in `config.json`:

| Layer | Default |
|---|---|
| Page delay | 4-9s, log-normal shaped |
| Reading pause | 30-90s every 5-8 pages |
| Session break | 3-8 min every 45-60 requests |
| Keyword gap | 20-60s, skipped entirely when a run is resumed |

Requests are strictly sequential with no concurrency. The user agent is fixed
for a run rather than rotated, since rotation mid-session is itself a tell. A
referer chain is maintained (page N cites page N-1), cookies persist as a
browser's would, `Retry-After` is honoured, and three consecutive failures
abort the run rather than hammering.

### robots.txt

`codecanyon.net/robots.txt` disallows `*?sort=*`, so sorted result URLs
(`?sort=sales`, `?sort=date`) are not crawled. Paginated search (`?page=N`) is
permitted and is the default.

Nothing is lost by this: sales, rating and last-updated appear on every Best
Match card, which is the optimisation the spec itself describes in section 7.
Setting `respect_robots: false` in `config.json` enables sort modes, and that
is a deliberate choice for the operator to make.

Note that `urllib.robotparser` cannot be used here — it fetches robots.txt with
Python's own user agent, receives a 403, and then fails closed by disallowing
every URL. It also does not support the wildcard patterns Envato uses. `ccr/robots.py`
implements Google-style matching instead.

## What gets collected

Every field below comes from the search results card, verified against the live
site on 2026-08-12:

`product_id` `title` `url` `author_name` `author_url` `category` `subcategory`
`price` `sales` `rating` `review_count` `software_version` `framework`
`compatible_with` `file_types` `last_updated`

**`published_date` is not available on search pages**, only `last_updated`.
Collecting it requires a visit to each item page — roughly one extra request
per product — so it is deliberately left out of the default crawl.

## Storage

SQLite is the source of truth at `research/db/research.sqlite`; the CSVs from
spec section 15 will be generated from it in Phase 5.

| Table | Holds |
|---|---|
| `research_runs` | one row per research run |
| `keywords` | what was searched and why (spec section 24) |
| `crawl_pages` | every page attempt — this is the resume ledger |
| `products` | one row per unique product, latest values |
| `product_snapshots` | per-run values, so growth over time is answerable |
| `occurrences` | product x keyword x page x position (spec section 13) |
| `keyword_results` | per keyword totals, including zero-result keywords |

Raw HTML for every page is archived gzipped under `research/raw/<date>/<run_id>/`
and never modified. When Envato rotates its CSS class names, the parser can be
fixed and historical runs re-parsed without re-scraping.

## Output

`python run.py export` writes five CSVs, deliberately not one giant file:

| File | Contents |
|---|---|
| `csv/<run_id>/keywords.csv` | what was searched, and what it returned |
| `csv/<run_id>/products.csv` | one unique row per product |
| `csv/<run_id>/search_occurrences.csv` | where each product appeared |
| `csv/<run_id>/keyword_summary.csv` | statistics per keyword |
| `csv/research_runs.csv` | one row per run, across all runs |

Per-run files sit in their own directory so a later run never overwrites an
earlier one — that history is what makes growth analysis possible.

`python run.py report` writes a self-contained HTML report to
`research/reports/<run_id>.html`. The stylesheet is inlined and there are no
scripts or external requests, so the file can be moved or mailed and still
render.

## Reading the numbers

Sales figures are lifetime totals published by CodeCanyon, not rates. An old
product's total reflects years of accumulation rather than current demand,
which is why maintenance activity is reported alongside sales rather than
folded into them.

Revenue is sales x list price. It ignores discounts, Envato's cut and refunds,
so treat it as an order of magnitude rather than a figure.

Feature counts match against product **titles**, so they measure what vendors
think is worth advertising — close to, but not the same as, what the products
actually do.

Result counts are what the site's search returned on the day of the run.
Search relevance is Envato's judgement, not a complete inventory of a market.

## Design decisions worth knowing

**Cards are selected by `[data-price][data-item-id]`, not by class name.**
Envato's class names are build hashes that rotate on deploy; the data
attributes are behavioural and far more stable. `data-item-id` alone is
insufficient — it also appears on favourite and collection buttons, five times
per card.

**A missing sales element means zero sales, not unknown.** Twenty-nine of
thirty cards on the sample page had one. Treating the absence as NULL would
quietly bias every average in the analysis.

**Pagination stops on `<link rel="next">`**, which is authoritative, rather
than on a guess about the pagination widget or an item-count calculation.

**The parser fails loudly.** If cards are found but mostly unparseable, or if
no cards are found on a page the site says has results, it raises
`ParserHealthError` and stops the run. Silently writing four hundred rows of
nulls is the worse outcome.

**Keyword statistics are derived from storage, not from memory.** On a resumed
run most pages are skipped and parse nothing, so an in-memory counter would
report every keyword as zero-result and overwrite good data.

**Zero-result keywords are recorded, never dropped** (spec sections 21-22).
"`perfex quantum` returned 0 results" is a competition signal, not a failure.

**Zero-result keywords are recorded, never dropped** (spec sections 21-22).
"`perfex quantum` returned 0 results" is a competition signal, not a failure,
and it appears in the summary CSV and the report with zeroed statistics rather
than being omitted.

**Charts are single-series, one colour, with the value beside the bar.** An
in-bar label on a short bar either overflows or gets clipped. Every chart is
accompanied by the same numbers in a table, so nothing is reachable only
through the visual. The palette is validated for contrast and colour-vision
deficiency in both light and dark modes.

## The AI layer

Set the key in the environment; it is never written to disk, never logged and
never stored in a run's config snapshot:

```bash
setx OPENAI_API_KEY "sk-..."
```

Two things use it, and **both degrade rather than fail** when it is absent:

**Keyword generation** proposes a keyword set spanning broad, capability and
speculative bands — including keywords expected to return nothing, because an
empty result is evidence about competition. Every generated keyword is written
to `keywords.csv` with `approved=no`. A model cannot start a crawl on its own;
regenerating never changes an approval a human already set.

**Analysis** never receives raw HTML, URLs or screenshots. It receives a
compact digest — the keyword table, market overview, distribution, feature
table, author concentration and top products — plus the caveats that stop the
numbers being misread. Two files are always written:

| File | Purpose |
|---|---|
| `analysis/<run_id>/dataset.md` | the digest |
| `analysis/<run_id>/prompt.md` | digest plus full instructions, ready to paste anywhere |
| `analysis/<run_id>/analysis.md` | written by gpt-4o-mini, when a key is present |

The bundle is the deliverable; the API call is a convenience on top of it. The
research outlives any one provider.

## Comparing runs

`python run.py diff` answers the spec section 16 question: what changed since
last time. Two traps it handles rather than falling into:

**Keyword scope.** Two runs rarely search the same keywords. If run A searched
3 and run B searched 8, a naive comparison reports every product unique to B
as new. Products are therefore compared only within the keywords **both** runs
searched, and keywords unique to either run are reported separately as scope
change.

**Pagination coverage.** Paginating a *live* result set can show one item on
two consecutive pages and skip another. Observed for real between the two runs
in this repo: `perfex integration` returned 150 results but one run captured
only 148 distinct products, and the two it missed then looked like new
arrivals in the next run. So the diff reports coverage gaps explicitly, and
flags any newly-seen product whose last-updated date predates the earlier run
as pre-existing rather than new.

Both mean small appear/disappear counts should be read as collection noise
unless coverage is complete and the dates support it.
