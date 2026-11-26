"use strict";
(() => {
  const { api, el, pct, $, toast, fan } = HZ;
  let FOCUS = "";
  let inboxStatus = "open";
  let THREAD = null;

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
      $("brief-sentence").textContent = "No data yet. An admin can start the first live collection from the admin page.";
      return false;
    }
    FOCUS = b.focus_brand;
    HZ.setFocus(FOCUS);
    $("fact-sov-k").textContent = `${FOCUS}'s share of voice`;
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
    $("run-meta").textContent = `Live YouTube data collected ${fmtDate(b.run.created_at)}: ${b.run.videos} result slots, ` +
      `${b.run.comments.toLocaleString("en-IN")} comments, ${b.run.quota_units_used} API quota units.`;
    $("evidence-link").href = `/api/v1/runs/${b.run.run_id}/evidence`;
    return true;
  }

  async function checkSetup() {
    const s = await api("/api/v1/setup");
    const msgs = [];
    if (!s.profile_configured) msgs.push("Nothing is tracked yet: an admin needs to choose a brand and its rivals on the admin page.");
    else if (!s.has_data) msgs.push(s.youtube_key_set
      ? "No collection yet: an admin can run the first live collection from the admin page."
      : "No collection yet, and the YouTube API key is not set on the server.");
    $("setup-banner").hidden = !msgs.length;
    $("setup-text").textContent = msgs.join(" ");
    return s;
  }

  async function loadVoice(keyword) {
    const data = await api(`/api/v1/sov?keyword=${encodeURIComponent(keyword)}`);
    const brands = data.report.brands;
    FOCUS = data.focus_brand;
    HZ.setFocus(FOCUS);
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
      ? `${FOCUS}: ${pct(me.mention_share)} of mentions, ${pct(me.engagement_share)} of engagement, ${pct(me.positive_share)} of positive comments, across ${data.report.videos} videos.`
      : `${FOCUS} was not mentioned for this search term.`;
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
    $("recs").replaceChildren(...data.insights.map((i) => {
      const cites = (i.citations || []).slice(0, 3).map((c, n) =>
        el("a", { href: c.url, target: "_blank", rel: "noopener", class: "cite", "data-testid": "citation",
          title: c.type === "comment" ? c.text : c.title }, `[${n + 1}]`));
      const badge = i.verified ? el("span", { class: "ok", title: "Numbers checked against the evidence" }, "verified") : null;
      return el("li", { "data-testid": `insight-${i.id}` }, el("strong", {}, i.title), i.text, " ", ...cites,
        ...(badge ? [" ", badge] : []));
    }));
  }

  // A safe subset of Markdown: "- " lists and **bold**, built as DOM nodes (never innerHTML).
  function renderAnswer(box, text) {
    const inline = (line) => line.split(/(\*\*[^*]+\*\*)/).filter(Boolean)
      .map((part) => (part.startsWith("**") && part.endsWith("**") ? el("strong", {}, part.slice(2, -2)) : part));
    const nodes = [];
    let list = null;
    for (const raw of text.split(/\n+/)) {
      const line = raw.trim();
      if (!line) continue;
      if (/^[-*•]\s+/.test(line)) {
        if (!list) { list = el("ul", {}); nodes.push(list); }
        list.append(el("li", {}, ...inline(line.replace(/^[-*•]\s+/, ""))));
      } else {
        list = null;
        nodes.push(el("p", {}, ...inline(line)));
      }
    }
    box.replaceChildren(...nodes);
  }

  // POST /api/v1/ask/stream and call onStep for each agent step; resolves with the final result.
  async function askStream(body, onStep) {
    const r = await fetch("/api/v1/ask/stream", {
      method: "POST", credentials: "same-origin",
      headers: { "X-Requested-With": "fanvaaz", "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.status === 401) { location.href = "/login?next=/app"; throw new Error("Signed out"); }
    if (!r.ok || !r.body) {
      let msg = `Request failed (${r.status})`;
      try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : msg; } catch { /* not JSON */ }
      throw new Error(msg);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "", result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const kind = (block.match(/^event: (.+)$/m) || [])[1];
        const data = (block.match(/^data: (.*)$/m) || [])[1];
        if (!kind || data === undefined) continue;
        const payload = JSON.parse(data);
        if (kind === "step") onStep(payload);
        else if (kind === "result") result = payload;
        else if (kind === "error") throw new Error(payload.detail || "Something went wrong");
      }
    }
    if (!result) throw new Error("The answer stream ended early. Try again.");
    return result;
  }

  const STEP_LABEL = { guard: "Checking the question", route: "Choosing how to answer", recall: "Reading the conversation",
    plan: "Planning", agent: "Looking up the data", stats: "Computing the numbers", cache: "Checking earlier answers",
    retrieve: "Finding comments", context: "Selecting evidence", answer: "Writing the answer", verify: "Checking numbers and citations",
    judge: "Checking each claim", help: "Explaining", general: "Answering from general knowledge" };

  let LAST = null;

  function showAnswer(q, data) {
    const out = $("ask-answer");
    renderAnswer(out, data.answer);
    out.className = "answer" + (data.refused ? " refused" : "");
    out.dataset.refused = String(data.refused);
    out.dataset.mode = data.mode;
    out.dataset.route = data.route || "";
    const badges = [{ llm: "Gemini, checked", guard: data.route === "off_topic" ? "Outside what I can answer" : "Refused by the guardrail",
      general: "General knowledge, not from your data", help: "About Fanvaaz" }[data.mode] || "From the data"];
    if (data.faithfulness != null) badges.push(`claims supported: ${Math.round(100 * data.faithfulness)}%`);
    if (data.cache && data.cache.hit) badges.push(`cached (similarity ${data.cache.similarity})`);
    if (data.scope && data.scope.brands) badges.push(`scope: ${data.scope.brands.join(", ")}` + (data.scope.aspect ? ` · ${data.scope.aspect}` : ""));
    if (data.fallback_reason) badges.push(`LLM answer rejected: ${data.fallback_reason}`);
    $("ask-badges").replaceChildren(...badges.map((b) => el("span", { class: "badge" }, b)));
    $("ask-sources").replaceChildren(...data.sources.slice(0, 6).map((s) =>
      el("li", { "data-testid": "ask-source" }, el("span", { class: "cid" }, s.citation || ""), " ",
        el("a", { href: `https://www.youtube.com/watch?v=${s.video_id}&lc=${s.comment_id}`, target: "_blank", rel: "noopener" }, s.text))));
    $("ask-steps").replaceChildren(...(data.reasoning_steps || []).map((s) => el("li", {}, s)));
    $("ask-reasoning").hidden = !(data.reasoning_steps || []).length;
    $("ask-followups").replaceChildren(...(data.follow_ups || []).map((f) => el("button", { type: "button", class: "pill", "data-testid": "follow-up",
      onclick: () => { $("ask-input").value = f; $("ask-form").requestSubmit(); } }, f)));
    $("ask-actions").hidden = !!data.refused;
    $("ask-up").disabled = $("ask-down").disabled = false;
    $("ask-thanks").textContent = "";
    LAST = { q, data };
  }

  async function run(q, fresh) {
    const out = $("ask-answer");
    const prog = $("ask-progress");
    out.textContent = "Looking through the comments…";
    out.className = "answer";
    $("ask-actions").hidden = true;
    $("ask-followups").replaceChildren();
    prog.replaceChildren();
    prog.hidden = false;
    const body = { question: q, ...(THREAD ? { thread_id: THREAD } : {}), ...(fresh ? { fresh: true } : {}) };
    try {
      const data = await askStream(body, (step) => {
        const node = step.split(":")[0];
        prog.append(el("li", { title: step }, STEP_LABEL[node] || node));
      });
      THREAD = data.thread_id;
      $("ask-new").hidden = false;
      showAnswer(q, data);
      return true;
    } catch (err) {
      out.textContent = err.message;
      return false;
    } finally {
      prog.hidden = true;
    }
  }

  async function ask(e) {
    e.preventDefault();
    const q = $("ask-input").value.trim();
    if (!q) return;
    if (await run(q, false)) {
      $("ask-thread").append(el("li", { class: "turn" }, q));
      $("ask-input").value = "";
    }
  }

  async function feedback(rating) {
    if (!LAST || !LAST.data.trace_id) return;
    $("ask-up").disabled = $("ask-down").disabled = true;
    try {
      await api("/api/v1/ask/feedback", { method: "POST", body: JSON.stringify({ trace_id: LAST.data.trace_id, rating,
        question: LAST.q, answer: LAST.data.answer.slice(0, 4000), mode: LAST.data.mode, route: LAST.data.route || "" }) });
      $("ask-thanks").textContent = rating > 0 ? "Thanks, noted." : "Thanks. This answer is kept for review.";
    } catch (err) {
      $("ask-thanks").textContent = err.message;
      $("ask-up").disabled = $("ask-down").disabled = false;
    }
  }

  function newConversation() {
    if (THREAD) api(`/api/v1/ask/threads/${THREAD}`, { method: "DELETE" }).catch(() => {});
    THREAD = null;
    LAST = null;
    $("ask-thread").replaceChildren();
    $("ask-answer").textContent = "";
    $("ask-badges").replaceChildren();
    $("ask-sources").replaceChildren();
    $("ask-followups").replaceChildren();
    $("ask-actions").hidden = true;
    $("ask-reasoning").hidden = true;
    $("ask-new").hidden = true;
  }

  async function loadProfile() {
    const p = await api("/api/v1/profile");
    document.title = `Fanvaaz: ${p.name}`;
    $("ask-input").placeholder = `What do people say about ${p.focus_brand} and its ${p.category}?`;
    const rival = p.rivals[0] || p.focus_brand;
    const qs = [`Why do people complain about ${p.focus_brand}?`, `${p.focus_brand} vs ${rival} on price`, `What do viewers praise about ${rival}?`];
    $("ask-suggest").replaceChildren(...qs.map((q) => el("button", { type: "button", class: "pill", "data-testid": "suggestion",
      onclick: () => { $("ask-input").value = q; $("ask-form").requestSubmit(); } }, q)));
  }

  async function init() {
    $("signout").addEventListener("click", HZ.signOut);
    $("ask-form").addEventListener("submit", ask);
    $("ask-new").addEventListener("click", newConversation);
    $("ask-up").addEventListener("click", () => feedback(1));
    $("ask-down").addEventListener("click", () => feedback(-1));
    $("ask-regen").addEventListener("click", () => { if (LAST) run(LAST.q, true); });
    try {
      await loadMe();
      const setup = await checkSetup();
      if (setup.profile_configured) await loadProfile();
      if (setup.has_data && await loadBrief()) await Promise.all([loadKeywords(), initInbox(), loadPlan()]);
    } catch (e) {
      if (e.message !== "Signed out") $("brief-sentence").textContent = e.message;
    }
    document.body.dataset.ready = "true";
  }
  init();
})();
