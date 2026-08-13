/* Dashboard front end. No framework, no build step, no external requests. */

const $ = (sel) => document.querySelector(sel);
const state = { runId: null, report: null, groups: [],
                sort: { key: "position", dir: 1 }, poll: null,
                busy: false, ai: null, draftCount: 0 };

const emptyState = (title) => `
  <div class="empty">
    <b>${esc(title)}</b>
    <p>Pick a past run from the menu at the top right, or start a new one on
       the <b>Collect</b> tab.</p>
  </div>`;

const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (v) => (v === null || v === undefined || v === "") ? "-"
  : (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : esc(v));

const money = (v) => (v === null || v === undefined) ? "-" : "$" + Number(v).toLocaleString();

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

/* --------------------------------------------------------------- components */

function bars(rows, maxValue) {
  if (!rows.length) return '<p class="muted">No data.</p>';
  const top = maxValue || Math.max(...rows.map((r) => r.value)) || 1;
  return '<div class="bars">' + rows.map((r) => {
    const width = Math.max(0, Math.min(100, (100 * (r.value || 0)) / top));
    return `<div class="bar-row">
      <div class="name" title="${esc(r.name)}">${esc(r.name)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${width.toFixed(1)}%"></div></div>
      <div class="val">${r.display}</div></div>`;
  }).join("") + "</div>";
}

function table(headers, rows, numeric = [], sortable = false, nowrap = []) {
  if (!rows.length) return '<p class="muted">No data.</p>';
  const head = headers.map((h, i) => {
    const cls = [numeric.includes(i) ? "num" : "", sortable ? "sortable" : ""].join(" ").trim();
    const key = sortable ? ` data-key="${esc(h.key || h)}"` : "";
    const label = esc(h.label || h);
    const arrow = sortable && state.sort.key === (h.key || h)
      ? `<span class="arrow">${state.sort.dir < 0 ? "▾" : "▴"}</span>` : "";
    return `<th class="${cls}"${key}>${label} ${arrow}</th>`;
  }).join("");
  const body = rows.map((cells) =>
    "<tr>" + cells.map((c, i) => {
      const cls = [numeric.includes(i) ? "num" : "",
                   nowrap.includes(i) ? "nowrap" : ""].join(" ").trim();
      return `<td class="${cls}">${c}</td>`;
    }).join("") + "</tr>"
  ).join("");
  return `<div class="scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function tiles(items) {
  return '<div class="tiles">' + items.map((t) =>
    `<div class="tile"><div class="label">${esc(t.label)}</div>
     <div class="value">${t.value}</div>
     <div class="note">${esc(t.note || "")}</div></div>`).join("") + "</div>";
}

/* ------------------------------------------------------------------ panels */

function renderOverview() {
  const r = state.report;
  if (!r || r.empty) {
    $("#panel-overview").innerHTML = emptyState(
      r?.message || "No run selected");
    return;
  }
  const o = r.overview;

  $("#panel-overview").innerHTML = `
    <section style="margin-top:0">
      <div class="card hero">
        <div class="label">Unique products found</div>
        <div class="value">${num(o.unique_products)}</div>
        <div class="label">across ${r.keywords.length} keywords &middot;
          ${esc(r.run.topic || "no topic")}</div>
      </div>
      ${tiles([
        { label: "Total sales", value: num(o.total_sales), note: "lifetime, all products" },
        { label: "Revenue estimate", value: money(Math.round(o.total_revenue_estimate)), note: "sales x list price, gross" },
        { label: "Median sales", value: num(o.median_sales), note: `average ${num(o.avg_sales)}` },
        { label: "Top seller", value: num(o.top_sales), note: "best-selling product" },
        { label: "Top 10 share", value: o.top10_share_of_sales + "%", note: "of all sales" },
        { label: "Never sold", value: num(o.products_without_sales), note: `of ${num(o.unique_products)} products` },
        { label: "Median price", value: money(o.median_price), note: `average ${money(o.avg_price)}` },
        { label: "Authors", value: num(o.unique_authors), note: "distinct sellers" },
      ])}
    </section>

    <section>
      <h2>Sales distribution</h2>
      <p class="sub">How sales are spread across products.</p>
      <div class="card">${bars(r.distribution.map((d) => ({
        name: d.bucket, value: d.products,
        display: `${num(d.products)} <small>${d.share}%</small>`,
      })))}</div>
    </section>

    <section>
      <h2>Author concentration</h2>
      <p class="sub">Share of total sales by seller.</p>
      <div class="card">${bars(r.authors.map((a) => ({
        name: a.author, value: a.share,
        display: `${num(a.sales)} <small>${a.share}%</small>`,
      })), 100)}</div>
    </section>

    <section>
      <h2>Maintenance</h2>
      <p class="sub">Search pages expose last-updated but not a published
        date, so this measures maintenance activity, not product age.</p>
      ${tiles([
        { label: "Unmaintained", value: num(r.age.unmaintained), note: `${r.age.unmaintained_share}% over a year old` },
        { label: "Old but still selling", value: num(r.age.old_still_selling), note: "stale, above median sales" },
        { label: "Fresh and selling", value: num(r.age.new_getting_sales), note: "updated <90d, with sales" },
        { label: "Low performers", value: num(r.age.low_performers), note: "5 sales or fewer" },
      ])}
    </section>

    <section>
      <h2>Products matching several keywords</h2>
      <p class="sub">Breadth of match is its own signal.</p>
      <div class="card">${table(
        ["Product", "Keywords", "Best rank", "Sales", "Matched"],
        r.cross_keyword.map((x) => [esc(x.title), num(x.keywords),
          "#" + x.best_position, num(x.sales),
          `<span class="muted">${esc(x.matched)}</span>`]),
        [1, 2, 3])}</div>
    </section>`;
}

function renderKeywords() {
  const r = state.report;
  if (!r || r.empty) {
    $("#panel-keywords").innerHTML = emptyState("No run selected");
    return;
  }

  const rows = r.keywords.map((k) => [
    `<span class="strong">${esc(k.keyword)}</span> ` +
      (k.zero_result ? '<span class="badge zero">zero results</span>' : ""),
    num(k.results), num(k.unique_products), num(k.total_sales),
    num(k.top_sales), num(k.median_sales), money(k.median_price),
  ]);

  $("#panel-keywords").innerHTML = `
    <section style="margin-top:0">
      <h2>Keywords</h2>
      <p class="sub">Keywords returning nothing are kept deliberately — an
        empty search says something about competition.</p>
      <div class="card">${bars(r.keywords.map((k) => ({
        name: k.keyword, value: k.results, display: num(k.results) + " results",
      })))}</div>
      <div class="card" style="margin-top:12px">${table(
        ["Keyword", "Results", "Unique", "Total sales", "Top", "Median", "Median price"],
        rows, [1, 2, 3, 4, 5, 6])}</div>
    </section>`;
}

function renderFeatures() {
  const r = state.report;
  if (!r || r.empty) {
    $("#panel-features").innerHTML = emptyState("No run selected");
    return;
  }

  const present = r.features.filter((f) => f.products);
  const absent = r.features.filter((f) => !f.products).map((f) => f.feature);

  $("#panel-features").innerHTML = `
    <section style="margin-top:0">
      <h2>Features by product count</h2>
      <p class="sub">Matched against product titles, so this measures what
        vendors advertise — close to, but not the same as, what products do.</p>
      <div class="card">${bars(present.slice(0, 14).map((f) => ({
        name: f.feature, value: f.products, display: num(f.products),
      })))}</div>
      <div class="card" style="margin-top:12px">${table(
        ["Feature", "Products", "Total sales", "Median", "Top", "Updated <90d", "Avg price"],
        present.map((f) => [`<span class="strong">${esc(f.feature)}</span>`,
          num(f.products), num(f.total_sales), num(f.median_sales),
          num(f.top_sales), num(f.recently_updated), money(f.avg_price)]),
        [1, 2, 3, 4, 5, 6])}
        ${absent.length ? `<p class="sub" style="margin-top:14px">No product
          title mentions: <b>${esc(absent.join(", "))}</b>. Absence is a
          candidate gap, not yet an opportunity.</p>` : ""}
      </div>
    </section>`;
}

function sortProducts(rows) {
  const { key, dir } = state.sort;
  return [...rows].sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (typeof x === "number" && typeof y === "number")
      ? (x - y) * dir : String(x).localeCompare(String(y)) * dir;
  });
}

function renderProducts() {
  if (!state.runId) {
    $("#product-groups").innerHTML = emptyState("No run selected");
    $("#product-count").textContent = "";
    return;
  }

  const query = ($("#product-search").value || "").toLowerCase().trim();
  const match = (p) => !query ||
    [p.title, p.author_name, p.category, p.subcategory, p.framework]
      .some((v) => (v || "").toLowerCase().includes(query));

  const headers = [
    { key: "position", label: "#" },
    { key: "title", label: "Product" }, { key: "author_name", label: "Author" },
    { key: "price", label: "Price" }, { key: "sales", label: "Sales" },
    { key: "rating", label: "Rating" }, { key: "review_count", label: "Reviews" },
    { key: "category", label: "Category" },
    { key: "last_updated", label: "Updated" },
  ];

  let shown = 0, total = 0;
  const blocks = [];

  for (const group of state.groups) {
    total += group.products.length;
    const rows = sortProducts(group.products.filter(match));
    shown += rows.length;

    // A keyword filtered down to nothing is hidden; one that genuinely found
    // nothing stays visible, because an empty search is itself a result.
    if (!rows.length && query) continue;

    blocks.push(`
      <div class="group-head">
        <span class="name">${esc(group.keyword)}</span>
        <span class="count">${num(rows.length)}
          ${rows.length === group.products.length ? "" : `of ${num(group.products.length)} `}
          product${rows.length === 1 ? "" : "s"}</span>
      </div>
      <div class="card">${rows.length ? table(headers, rows.map((p) => [
        `<span class="muted">${num(p.position)}</span>`,
        // The real URL, not one rebuilt from the id: CodeCanyon item paths
        // carry a slug and a rebuilt guess could not be verified as valid.
        `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>`,
        esc(p.author_name), money(p.price), num(p.sales),
        p.rating ?? "-", num(p.review_count),
        `<span class="muted">${esc(p.subcategory || p.category || "")}</span>`,
        esc(p.last_updated),
      ]), [0, 3, 4, 5, 6], true, [8])
      : '<p class="muted">This search returned nothing — a competition signal, not a gap in the data.</p>'}</div>`);
  }

  $("#product-count").textContent = query
    ? `${num(shown)} of ${num(total)} rows` : `${num(total)} rows`;
  $("#product-groups").innerHTML = blocks.join("")
    || emptyState("Nothing matches that filter");

  $("#product-groups").querySelectorAll("th.sortable").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.key;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : -1 };
      renderProducts();
    };
  });
}

/* ------------------------------------------------------------------ compare */

function renderDiff(d) {
  if (!d || d.empty) {
    $("#diff-body").innerHTML =
      `<p class="muted">${esc(d?.message || "Nothing to compare.")}</p>`;
    return;
  }

  const t = d.totals;
  const scope = d.scope;
  const sign = (n) => (n > 0 ? "+" : "") + num(n);

  const scopeWarning = scope.matches ? "" : `
    <div class="card" style="margin-bottom:12px">
      <p class="sub" style="margin:0"><b>The two runs did not search the same
      keywords.</b> Comparison is restricted to the ${scope.shared.length}
      shared keyword(s), so these counts are scope-limited and are not
      whole-market movements.
      ${scope.only_in_a.length ? `<br>Only in the earlier run:
        <b>${esc(scope.only_in_a.join(", "))}</b>.` : ""}
      ${scope.only_in_b.length ? `<br>Only in the later run:
        <b>${esc(scope.only_in_b.join(", "))}</b>.` : ""}
      </p>
    </div>`;

  const coverage = !d.coverage?.length ? "" : `
    <div class="card" style="margin-bottom:12px">
      <p class="sub" style="margin:0 0 8px"><b>Incomplete pagination
      coverage.</b> These keywords returned more results than the run captured
      as distinct products. Paginating a live result set can repeat one item
      across two pages and skip another, so appear/disappear counts of this
      size are collection noise, not market movement.</p>
      ${table(["Keyword", "Run", "Results", "Captured", "Missed"],
        d.coverage.map((c) => [esc(c.keyword), esc(c.run), num(c.results),
          num(c.captured), num(c.missed)]), [2, 3, 4])}
    </div>`;

  const gainers = d.changed.filter((c) => c.sales_delta > 0);

  $("#diff-body").innerHTML = `
    ${scopeWarning}
    ${tiles([
      { label: "Products", value: sign(t.products_delta),
        note: `${num(t.products_before)} → ${num(t.products_after)}` },
      { label: "Sales", value: sign(t.sales_delta),
        note: `${num(t.sales_before)} → ${num(t.sales_after)}` },
      { label: "Newly seen", value: num(t.appeared),
        note: t.appeared_existing
          ? `${t.appeared_existing} existed before` : "none pre-existing" },
      { label: "No longer found", value: num(t.disappeared), note: "" },
      { label: "Changed", value: num(t.changed), note: "sales or price moved" },
    ])}
    ${coverage}

    <section>
      <h2>Result counts per keyword</h2>
      <div class="card">${table(
        ["Keyword", "Before", "After", "Change"],
        d.keywords.map((k) => [esc(k.keyword), num(k.results_before),
          num(k.results_after), sign(k.results_delta)]), [1, 2, 3])}</div>
    </section>

    <section>
      <h2>Biggest sales gains</h2>
      <div class="card">${table(
        ["Product", "Before", "After", "Change", "Price change"],
        gainers.slice(0, 25).map((c) => [
          `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>`,
          num(c.sales_before), num(c.sales_after), sign(c.sales_delta),
          c.price_delta ? sign(c.price_delta) : "-"]), [1, 2, 3, 4])}</div>
    </section>

    <section>
      <h2>Newly seen products</h2>
      <p class="sub">A product last updated before the earlier run already
        existed — it was newly ranked into scope or missed by that run's
        pagination, not a new arrival.</p>
      <div class="card">${table(
        ["Product", "Sales", "Last updated", "Status"],
        d.appeared.map((p) => [esc(p.title), num(p.sales),
          esc(p.last_updated),
          p.existed_before
            ? '<span class="badge">existed before</span>'
            : '<span class="badge fresh">new</span>']), [1])}</div>
    </section>

    <section>
      <h2>No longer found</h2>
      <div class="card">${table(["Product", "Sales"],
        d.disappeared.map((p) => [esc(p.title), num(p.sales)]), [1])}</div>
    </section>`;
}

async function loadDiff() {
  const from = $("#diff-from").value;
  const to = $("#diff-to").value;
  if (!from || !to) { renderDiff({ empty: true, message: "Need two runs." }); return; }
  renderDiff(await api(`/api/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`));
}

/* ------------------------------------------------------------------ collect */

function updateStartButton() {
  // Startable only with approved keywords in the draft and nothing running.
  const btn = $("#start-btn");
  btn.disabled = state.busy || !state.draftCount;
  btn.title = state.busy ? "a run is already in progress"
    : (state.draftCount ? "" : "add and approve at least one keyword first");
}

async function renderKeywordList() {
  // With a run selected, show what that run searched. With none, show the
  // editable draft for the next one. They are different things: the draft
  // changes between runs, so it cannot stand in for a run's own record.
  const suffix = state.runId ? `?run=${encodeURIComponent(state.runId)}` : "";
  const payload = await api("/api/keywords" + suffix);

  const viewingRun = payload.mode === "run";
  $("#run-keywords").classList.toggle("hidden", !viewingRun);
  $("#draft-keywords").classList.toggle("hidden", viewingRun);

  if (viewingRun) return renderRunKeywords(payload);
  return renderDraftKeywords(payload.keywords);
}

function renderRunKeywords(payload) {
  const rows = payload.keywords;
  const zero = rows.filter((r) => r.zero_result).length;

  $("#run-keywords-sub").innerHTML =
    `Run <b>${esc(payload.run_id)}</b> searched ${num(rows.length)}
     keyword${rows.length === 1 ? "" : "s"}${payload.topic
       ? ` under the topic <b>${esc(payload.topic)}</b>` : ""}.
     ${zero ? `${num(zero)} returned nothing, which is itself a
       competition signal.` : ""}
     This is the permanent record of what was searched — it does not change
     when you edit the draft list for a new run.`;

  $("#run-keyword-list").innerHTML = table(
    ["Keyword", "Topic", "Results", "Products", "Pages", "Source", "Priority"],
    rows.map((k) => [
      `<span class="strong">${esc(k.keyword)}</span>` +
        (k.zero_result ? ' <span class="badge zero">zero results</span>' : ""),
      esc(k.parent_topic || ""), num(k.total_results),
      num(k.unique_products), num(k.pages_crawled),
      esc(k.source || ""), esc(k.priority || ""),
    ]), [2, 3, 4]);

  $("#reuse-status").textContent = "";
}

function renderDraftKeywords(rows) {
  const ai = state.ai || { available: false, env_var: "OPENAI_API_KEY" };

  $("#generate-btn").disabled = !ai.available;
  $("#generate-btn").title = ai.available
    ? `Generate with ${ai.model}`
    : `${ai.env_var} is not set — add keywords by hand or paste a list`;

  const approved = rows.filter((r) => r.approved).length;
  $("#keyword-status").textContent = rows.length
    ? `${approved} of ${rows.length} approved`
      + (ai.available ? "" : ` · ${ai.env_var} not set`)
    : "empty — add or paste keywords to begin";

  state.draftCount = approved;
  $("#clear-btn").disabled = !rows.length;
  updateStartButton();

  $("#keyword-list").innerHTML = rows.length ? table(
    ["Keyword", "Topic", "Source", "Approved", "Priority", ""],
    rows.map((k) => [
      `<span class="strong">${esc(k.keyword)}</span>`,
      esc(k.parent_topic || ""), esc(k.source),
      k.approved ? '<span class="badge fresh">yes</span>'
                 : '<span class="badge">no</span>',
      esc(k.priority || ""),
      `<button data-kw="${esc(k.keyword)}" data-approve="${!k.approved}">
        ${k.approved ? "Unapprove" : "Approve"}</button>
       <button data-kw="${esc(k.keyword)}" data-remove="true">Remove</button>`,
    ]))
    : '<p class="muted">No keywords yet. Add one above, paste a list, or '
      + 'reuse the keywords from a past run.</p>';

  $("#keyword-list").querySelectorAll("button[data-kw]").forEach((btn) => {
    btn.onclick = async () => {
      const path = btn.dataset.remove === "true"
        ? "/api/keywords/delete" : "/api/keywords/approve";
      await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords: [btn.dataset.kw],
                               approved: btn.dataset.approve === "true" }),
      });
      renderKeywordList();
    };
  });
}

const SIZE = (n) => n < 1024 ? `${n} B`
  : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB`
  : `${(n / 1024 / 1024).toFixed(1)} MB`;

async function renderOutputs() {
  if (!state.runId) {
    $("#outputs").innerHTML = '<p class="muted">Select a run above, or finish ' +
      'a new one, to see the files it produced.</p>';
    return;
  }
  const res = await api(`/api/outputs?run=${encodeURIComponent(state.runId)}`);
  if (!res.files.length) {
    $("#outputs").innerHTML =
      '<p class="muted">Nothing generated yet. Use “Export CSVs + report” or ' +
      '“Build analysis bundle”.</p>';
    return;
  }
  $("#outputs").innerHTML = table(
    ["File", "Kind", "Size", ""],
    res.files.map((f) => [
      `<span class="strong">${esc(f.name)}</span>`,
      `<span class="muted">${esc(f.kind)}</span>`,
      SIZE(f.bytes),
      `<a href="/view/${esc(f.path)}" target="_blank" rel="noopener">open</a>
       &nbsp;·&nbsp;
       <a href="/download/${esc(f.path)}">download</a>`,
    ]), [2]);
}

async function pollProgress() {
  const p = await api("/api/progress");
  $("#log").textContent = (p.log || []).join("\n") || "Idle.";
  $("#log").scrollTop = $("#log").scrollHeight;
  $("#collect-status").textContent = p.busy
    ? `running ${p.run_id}` : (p.run_id ? `${p.status} — ${p.run_id}` : "");
  state.busy = p.busy;
  updateStartButton();

  if (!p.busy && state.poll) {
    clearInterval(state.poll);
    state.poll = null;
    // Select the run that just finished -- you asked for it, so show it.
    state.runId = p.run_id || null;
    await loadRuns({ keepSelection: true });
    await renderKeywordList();
    renderOutputs();
  }
}

/* --------------------------------------------------------------------- boot */

async function loadReport(runId) {
  state.runId = runId || null;

  if (!state.runId) {
    // Nothing selected: show empty panels rather than silently presenting
    // some previous run's numbers as if they were current.
    state.report = { empty: true, message: "No run selected" };
    state.groups = [];
    $("#run-meta").innerHTML =
      "no run selected &middot; choose one at the top right, or start a new one";
    renderOverview(); renderKeywords(); renderFeatures(); renderProducts();
    return;
  }

  // An explicit empty `run=` tells the server "none", not "pick a default".
  const suffix = `?run=${encodeURIComponent(state.runId)}`;
  const [report, products] = await Promise.all([
    api("/api/report" + suffix), api("/api/products" + suffix),
  ]);
  state.report = report;
  state.groups = products.groups || [];

  const run = report.run || {};
  $("#run-meta").innerHTML = report.empty ? "No run selected"
    : `run <b>${esc(run.run_id)}</b> &middot; ${esc(run.status || "")} &middot;
       started ${esc((run.started_at || "").replace("T", " ").slice(0, 16))}`;

  renderOverview(); renderKeywords(); renderFeatures(); renderProducts();
}

async function loadRuns({ keepSelection = false } = {}) {
  const runs = await api("/api/runs");
  const options = runs.map((r) =>
    `<option value="${esc(r.run_id)}">${esc(r.run_id)} — ${esc(r.topic || "no topic")}
     (${r.unique_products} products)${r.status === "completed" ? "" : ` — ${esc(r.status)}`}
     </option>`).join("");

  // No run is selected on open. Past data is a deliberate choice, not the
  // thing that greets you: the landing state is "start a new run".
  $("#run-picker").innerHTML =
    '<option value="">— select a run to view —</option>' + options;
  $("#diff-from").innerHTML = options || '<option value="">no runs yet</option>';
  $("#diff-to").innerHTML = options || '<option value="">no runs yet</option>';

  const complete = runs.filter((r) => r.status === "completed");
  if (complete.length > 1) {
    $("#diff-from").value = complete[complete.length - 1].run_id;
    $("#diff-to").value = complete[0].run_id;
  } else if (runs.length > 1) {
    $("#diff-from").value = runs[runs.length - 1].run_id;
    $("#diff-to").value = runs[0].run_id;
  }

  const selection = keepSelection && state.runId ? state.runId : "";
  $("#run-picker").value = selection;
  await loadReport(selection || null);
}

function initTabs() {
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll('[role="tab"]').forEach((t) =>
        t.setAttribute("aria-selected", String(t === tab)));
      document.querySelectorAll(".panel").forEach((p) =>
        p.classList.add("hidden"));
      $("#panel-" + tab.dataset.tab).classList.remove("hidden");
      if (tab.dataset.tab === "collect") { renderKeywordList(); renderOutputs(); }
      if (tab.dataset.tab === "compare") loadDiff();
    };
  });
}

function initControls() {
  $("#run-picker").onchange = async (e) => {
    await loadReport(e.target.value);
    // The Collect tab is run-dependent too: it shows either that run's
    // keyword record or the draft for a new one.
    await renderKeywordList();
    renderOutputs();
  };
  $("#product-search").oninput = renderProducts;

  $("#export-btn").onclick = async () => {
    $("#export-btn").disabled = true;
    try {
      const res = await api("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: state.runId }),
      });
      $("#export-btn").textContent = res.ok
        ? `Exported ${res.files.length} files + report` : `Failed: ${res.error}`;
      renderOutputs();
    } finally {
      setTimeout(() => {
        $("#export-btn").disabled = false;
        $("#export-btn").textContent = "Export CSVs + report";
      }, 3000);
    }
  };

  $("#diff-btn").onclick = loadDiff;
  $("#diff-from").onchange = loadDiff;
  $("#diff-to").onchange = loadDiff;

  const addKeyword = async () => {
    const keyword = $("#new-keyword").value.trim();
    if (!keyword) return;
    const res = await api("/api/keywords/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword, topic: $("#new-topic").value.trim() }),
    });
    if (res.ok) $("#new-keyword").value = "";
    // renderKeywordList rewrites the status line, so set the message after it.
    await renderKeywordList();
    $("#keyword-status").textContent = res.ok ? `added “${keyword}”` : res.error;
  };

  $("#add-keyword-btn").onclick = addKeyword;
  $("#new-keyword").onkeydown = (e) => { if (e.key === "Enter") addKeyword(); };

  $("#bulk-add-btn").onclick = async () => {
    const text = $("#bulk-text").value;
    if (!text.trim()) { $("#bulk-status").textContent = "paste some keywords first"; return; }

    $("#bulk-add-btn").disabled = true;
    try {
      const res = await api("/api/keywords/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, topic: $("#bulk-topic").value.trim() }),
      });
      if (!res.ok) { $("#bulk-status").textContent = res.error; return; }

      await renderKeywordList();
      const skipped = res.skipped.length
        ? `, ${res.skipped.length} already present` : "";
      $("#bulk-status").textContent =
        `added ${res.added.length} of ${res.parsed} keywords${skipped}`;
      if (res.added.length) $("#bulk-text").value = "";
    } finally {
      $("#bulk-add-btn").disabled = false;
    }
  };

  $("#generate-btn").onclick = async () => {
    const topic = $("#gen-topic").value.trim();
    if (!topic) { $("#keyword-status").textContent = "enter a topic first"; return; }
    $("#generate-btn").disabled = true;
    $("#keyword-status").textContent = "generating…";
    try {
      const res = await api("/api/keywords/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic }),
      });
      if (!res.ok) { $("#keyword-status").textContent = res.error; return; }
      await renderKeywordList();
      $("#keyword-status").textContent =
        `added ${res.added.length} unapproved keywords — review and approve`;
    } finally {
      $("#generate-btn").disabled = false;
    }
  };

  $("#approve-all-btn").onclick = async () => {
    await api("/api/keywords/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved: true }),
    });
    renderKeywordList();
  };

  $("#clear-btn").onclick = async () => {
    const res = await api("/api/keywords/clear", { method: "POST" });
    await renderKeywordList();
    $("#keyword-status").textContent =
      `cleared ${res.removed} keyword${res.removed === 1 ? "" : "s"}`;
  };

  $("#reuse-btn").onclick = async () => {
    $("#reuse-btn").disabled = true;
    try {
      const res = await api("/api/keywords/reuse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: state.runId }),
      });
      if (!res.ok) { $("#reuse-status").textContent = res.error; return; }
      // Switch to the new-run view so the copied draft is what you see.
      $("#run-picker").value = "";
      await loadReport(null);
      await renderKeywordList();
      renderOutputs();
      $("#keyword-status").textContent =
        `copied ${res.added.length} keyword${res.added.length === 1 ? "" : "s"}`
        + (res.skipped.length ? `, ${res.skipped.length} already in the draft` : "");
      if (res.topic) $("#topic-input").value = res.topic;
    } finally {
      $("#reuse-btn").disabled = false;
    }
  };

  $("#analyze-btn").onclick = async () => {
    if (!state.runId) {
      $("#analyze-status").textContent = "select a run to analyse first";
      return;
    }
    $("#analyze-btn").disabled = true;
    $("#analyze-status").textContent = "building bundle…";
    try {
      const res = await api("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: state.runId }),
      });
      $("#analyze-status").textContent = res.ok
        ? (res.analysis ? `wrote ${res.analysis}` : (res.reason || "bundle written"))
        : res.error;
      renderOutputs();
    } finally {
      $("#analyze-btn").disabled = false;
    }
  };

  $("#start-btn").onclick = async () => {
    $("#start-btn").disabled = true;
    const res = await api("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: $("#topic-input").value || null }),
    });
    if (!res.ok) {
      $("#collect-status").textContent = res.error;
      $("#start-btn").disabled = false;
      return;
    }
    if (state.poll) clearInterval(state.poll);
    state.poll = setInterval(pollProgress, 1500);
    pollProgress();
  };
}

initTabs();
initControls();

// Opens on Collect with nothing selected: the landing state is "start a run",
// not somebody else's numbers.
(async () => {
  try {
    state.ai = await api("/api/ai-status");
    await loadRuns();
    await renderKeywordList();
    await renderOutputs();
    await pollProgress();
  } catch (err) {
    $("#run-meta").textContent = "Error: " + err.message;
  }
})();
