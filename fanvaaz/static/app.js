"use strict";
(() => {
  const { api, el, pct, $, toast, fan, FOCUS } = HZ;
  let inboxStatus = "open";

  function fmtDate(iso) {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  async function loadMe() {
    const me = await api("/api/v1/auth/me");
    $("user-name").textContent = `Signed in as ${me.name}`;
    $("admin-link").hidden = me.role !== "admin";
  }

  async function loadBrief() {
    const b = await api("/api/v1/brief");
    if (!b.has_data) {
      $("brief-sentence").textContent = "No data yet. An admin can start the first run from the admin page.";
      return;
    }
    $("brief-sentence").textContent = b.sentence;
    $("fact-sov").textContent = pct(b.sov);
    const d = $("fact-delta");
    if (b.sov_delta == null) { d.textContent = "First run"; d.className = "v"; }
    else {
      const pts = 100 * b.sov_delta;
      d.textContent = `${pts > 0 ? "+" : pts < 0 ? "−" : "±"}${Math.abs(pts).toFixed(1)} pts`;
      d.className = "v " + (pts > 0.05 ? "delta-up" : pts < -0.05 ? "delta-down" : "");
    }
    $("fact-rank").textContent = b.rank ? `#${b.rank} of ${b.brands_ranked}` : "–";
    $("fact-replies").textContent = String(b.open_risks);
    const next = $("next-action");
    if (b.action) {
      next.replaceChildren(el("strong", {}, b.action.title), b.action.text);
      next.hidden = false;
    }
    $("demo-banner").hidden = b.run.source !== "synthetic-demo";
    $("run-meta").textContent = `Last collected ${fmtDate(b.run.created_at)} from ${b.run.videos} result slots and ${b.run.comments.toLocaleString("en-IN")} comments.`;
  }

  async function loadVoice(keyword) {
    const data = await api(`/api/v1/sov?keyword=${encodeURIComponent(keyword)}`);
    const brands = data.report.brands;
    fan($("sov-fan"), brands, { title: `Share of voice for ${keyword === "all" ? "all search terms" : keyword}` });
    const max = Math.max(...brands.map((b) => b.sov), 0.0001);
    $("rank-list").replaceChildren(...brands.map((b, i) => {
      const fill = el("div", { class: "fill" });
      fill.style.width = `${(100 * b.sov) / max}%`;
      return el("li", { class: b.brand === FOCUS ? "focus" : "", "data-testid": `rank-${b.brand}` },
        el("span", { class: "pos" }, String(i + 1)), el("span", {}, b.brand),
        el("div", { class: "track" }, fill), el("span", { class: "val" }, pct(b.sov)));
    }));
    const me = brands.find((b) => b.brand === FOCUS);
    $("breakdown").textContent = me
      ? `Atomberg: ${pct(me.mention_share)} of mentions, ${pct(me.engagement_share)} of engagement, ${pct(me.positive_share)} of positive comments, across ${data.report.videos} videos.`
      : "Atomberg was not mentioned for this search term.";
  }

  async function loadKeywords() {
    const ks = await api("/api/v1/keywords");
    const box = $("keyword-pills");
    const pick = (k) => {
      box.querySelectorAll(".pill").forEach((p) => p.setAttribute("aria-pressed", String(p.dataset.k === k)));
      loadVoice(k);
    };
    box.replaceChildren(...ks.map((k) => el("button", { type: "button", class: "pill", "data-k": k,
      "data-testid": `kw-${k}`, "aria-pressed": String(k === "all"), onclick: () => pick(k) }, k === "all" ? "All terms" : k)));
    await loadVoice("all");
  }

  function inboxItem(f) {
    const li = el("li", { class: "item", "data-testid": "inbox-item" });
    const act = async (status, label) => {
      try {
        await api("/api/v1/risks/status", { method: "PATCH", body: JSON.stringify({ comment_id: f.comment_id, brand: f.brand, status }) });
        li.classList.add("leaving");
        setTimeout(() => { li.remove(); if (!$("inbox").children.length) emptyInbox(); }, 260);
        toast(label);
        loadBrief();
      } catch (e) { toast(e.message); }
    };
    const acts = inboxStatus === "open"
      ? [el("button", { class: "btn small", type: "button", "data-testid": "mark-handled", onclick: () => act("handled", "Marked handled") }, "Mark handled"),
         el("button", { class: "btn ghost small", type: "button", "data-testid": "dismiss", onclick: () => act("dismissed", "Dismissed") }, "Dismiss")]
      : [el("button", { class: "btn ghost small", type: "button", "data-testid": "reopen", onclick: () => act("open", "Moved back to open") }, "Reopen")];
    li.append(
      el("span", { class: `sev ${f.severity}` }, f.severity),
      el("div", {}, el("p", { class: "quote" }, `“${f.evidence}”`),
        el("p", { class: "meta" }, `${f.categories.map((c) => c.replace("_", " ")).join(", ")}. On “${f.keyword}”, ${f.like_count} likes.` +
          (f.updated_by ? ` Updated by ${f.updated_by}.` : ""))),
      el("div", { class: "acts" }, ...acts));
    return li;
  }

  function emptyInbox() {
    const msg = inboxStatus === "open" ? "Nothing waiting. New flags arrive with the next run." : `No ${inboxStatus} comments yet.`;
    $("inbox").replaceChildren(el("li", { class: "empty" }, msg));
  }

  const PAGE = 8;
  let shown = PAGE;
  async function loadInbox(reset = true) {
    if (reset) shown = PAGE;
    const brand = $("inbox-brand").value || FOCUS;
    const data = await api(`/api/v1/risks?brand=${encodeURIComponent(brand)}&status=${inboxStatus}&limit=${shown + 1}`);
    $("inbox-more").hidden = data.flags.length <= shown;
    if (!data.flags.length) return emptyInbox();
    $("inbox").replaceChildren(...data.flags.slice(0, shown).map(inboxItem));
  }

  async function initInbox() {
    const data = await api("/api/v1/risks?limit=1");
    const brands = Object.keys(data.summary).sort((a, b) => (a === FOCUS ? -1 : b === FOCUS ? 1 : a.localeCompare(b)));
    $("inbox-brand").replaceChildren(...brands.map((b) => el("option", { value: b }, b === FOCUS ? b : `${b} (competitor)`)));
    $("inbox-brand").addEventListener("change", () => loadInbox());
    $("inbox-more").addEventListener("click", () => { shown += PAGE; loadInbox(false); });
    const box = $("status-pills");
    const pick = (s) => {
      inboxStatus = s;
      box.querySelectorAll(".pill").forEach((p) => p.setAttribute("aria-pressed", String(p.dataset.s === s)));
      loadInbox();
    };
    box.replaceChildren(...[["open", "Waiting"], ["handled", "Handled"], ["dismissed", "Dismissed"]].map(([s, t]) =>
      el("button", { type: "button", class: "pill", "data-s": s, "data-testid": `status-${s}`, "aria-pressed": String(s === "open"), onclick: () => pick(s) }, t)));
    await loadInbox();
  }

  async function loadPlan() {
    const data = await api("/api/v1/insights");
    $("recs").replaceChildren(...data.insights.map((i) =>
      el("li", { "data-testid": `insight-${i.id}` }, el("strong", {}, i.title), i.text)));
  }

  async function ask(e) {
    e.preventDefault();
    const q = $("ask-input").value.trim();
    if (!q) return;
    const out = $("ask-answer");
    out.textContent = "Looking through the comments…";
    out.className = "answer";
    try {
      const data = await api("/api/v1/ask", { method: "POST", body: JSON.stringify({ question: q }) });
      out.textContent = data.answer;
      out.className = "answer" + (data.refused ? " refused" : "");
      out.dataset.refused = String(data.refused);
      $("ask-sources").replaceChildren(...data.sources.slice(0, 5).map((s) => el("li", {}, `${s.keyword}: ${s.text}`)));
    } catch (err) {
      out.textContent = err.message;
    }
  }

  async function init() {
    $("signout").addEventListener("click", HZ.signOut);
    $("ask-form").addEventListener("submit", ask);
    try {
      await loadMe();
      await loadBrief();
      await Promise.all([loadKeywords(), initInbox(), loadPlan()]);
    } catch (e) {
      if (e.message !== "Signed out") $("brief-sentence").textContent = e.message;
    }
    document.body.dataset.ready = "true";
  }
  init();
})();
