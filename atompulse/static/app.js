"use strict";
const FOCUS = "Atomberg";
const $ = (id) => document.getElementById(id);
const pct = (x) => `${(100 * x).toFixed(1)}%`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function el(tag, attrs = {}, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text; // textContent only: comments are untrusted
  return e;
}

function renderChart(brands) {
  const chart = $("chart");
  chart.replaceChildren();
  const max = Math.max(...brands.map((b) => b.sov), 0.0001);
  for (const b of brands) {
    const row = el("div", { class: "bar-row" + (b.brand === FOCUS ? " focus" : ""), "data-testid": `bar-${b.brand}` });
    row.append(el("span", {}, b.brand));
    const track = el("div", { class: "bar-track" });
    const bar = el("div", { class: "bar" });
    bar.style.width = `${(100 * b.sov) / max}%`;
    track.append(bar);
    row.append(track, el("span", { class: "bar-val" }, pct(b.sov)));
    chart.append(row);
  }
}

function renderTable(brands) {
  const body = $("brand-rows");
  body.replaceChildren();
  for (const b of brands) {
    const tr = el("tr", { class: b.brand === FOCUS ? "focus" : "", "data-testid": `row-${b.brand}` });
    const net = el("td", { class: b.net_sentiment >= 0 ? "pos" : "neg" }, b.net_sentiment.toFixed(2));
    tr.append(el("td", {}, b.brand), el("td", {}, pct(b.sov)), el("td", {}, pct(b.mention_share)),
      el("td", {}, pct(b.engagement_share)), el("td", {}, pct(b.positive_share)), net,
      el("td", {}, String(b.raw_mentions)));
    body.append(tr);
  }
}

function renderKpis(brands) {
  const i = brands.findIndex((b) => b.brand === FOCUS);
  const me = brands[i];
  $("kpi-sov").textContent = me ? pct(me.sov) : "0%";
  $("kpi-rank").textContent = me ? `#${i + 1} of ${brands.length}` : "–";
  $("kpi-pos").textContent = me ? pct(me.positive_share) : "–";
  $("kpi-net").textContent = me ? me.net_sentiment.toFixed(2) : "–";
}

async function loadKeyword(k) {
  const data = await api(`/api/v1/sov?keyword=${encodeURIComponent(k)}`);
  const r = data.report;
  $("run-meta").textContent = `${r.videos} videos · ${r.comments} comments · run ${data.run.run_id}`;
  $("demo-banner").hidden = data.run.source !== "synthetic-demo";
  renderKpis(r.brands);
  renderChart(r.brands);
  renderTable(r.brands);
}

async function loadInsights() {
  const data = await api("/api/v1/insights");
  const list = $("insights");
  list.replaceChildren();
  for (const ins of data.insights) {
    const li = el("li", { "data-testid": `insight-${ins.id}` });
    const head = el("strong");
    head.append(el("span", { class: "tag" }, ins.kind), document.createTextNode(ins.title));
    li.append(head, document.createTextNode(ins.text));
    list.append(li);
  }
}

async function ask(e) {
  e.preventDefault();
  const q = $("ask-input").value.trim();
  if (!q) return;
  const out = $("ask-answer");
  out.textContent = "Thinking…";
  out.className = "answer";
  try {
    const data = await api("/api/v1/ask", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }),
    });
    out.textContent = data.answer;
    out.className = "answer" + (data.refused ? " refused" : "");
    out.dataset.refused = String(data.refused);
    const src = $("ask-sources");
    src.replaceChildren(...data.sources.slice(0, 5).map((s) => el("li", {}, `${s.keyword}: ${s.text}`)));
  } catch (err) {
    out.textContent = `Error: ${err.message}`;
  }
}

async function init() {
  try {
    const keywords = await api("/api/v1/keywords");
    const sel = $("keyword");
    for (const k of keywords) sel.append(el("option", { value: k }, k === "all" ? "All keywords" : k));
    sel.addEventListener("change", () => loadKeyword(sel.value));
    await Promise.all([loadKeyword("all"), loadInsights()]);
  } catch (err) {
    $("run-meta").textContent = "No data yet: run `atompulse run --source demo`.";
  }
  $("ask-form").addEventListener("submit", ask);
  document.body.dataset.ready = "true";
}
init();
