"use strict";
(() => {
  const { api, el, $, toast } = HZ;
  let me = null;

  const fmt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Never");

  async function loadRuns() {
    const runs = await api("/api/v1/admin/runs");
    $("runs").replaceChildren(...runs.map((r) => el("tr", { "data-testid": "run-row" },
      el("td", {}, fmt(r.created_at)), el("td", {}, r.focus_brand), el("td", {}, r.source === "youtube" ? "YouTube (live)" : "Recorded YouTube"),
      el("td", { class: "num" }, String(r.videos)), el("td", { class: "num" }, r.comments.toLocaleString("en-IN")),
      el("td", { class: "num" }, String(r.quota_units_used)),
      el("td", {}, el("a", { href: `/api/v1/runs/${r.run_id}/evidence`, target: "_blank", rel: "noopener" }, "JSON")))));
  }

  const STEPS = ["collect", "extract", "relabel", "score", "risk", "insights", "rewrite", "evidence", "persist", "index"];

  function showProgress(current, done) {
    const box = $("run-progress");
    box.hidden = false;
    const at = STEPS.indexOf(current);
    box.replaceChildren(...STEPS.map((s, i) => el("li", {
      class: done || i < at ? "done" : i === at ? "now" : "", "data-testid": `step-${s}` }, s)));
  }

  async function startRun(btn) {
    $("run-error").textContent = "";
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Collecting live data…";
    try {
      let job = await api("/api/v1/runs/jobs", { method: "POST", body: JSON.stringify({ source: "youtube" }) });
      showProgress("collect");
      while (job.status === "queued" || job.status === "running") {
        await new Promise((r) => setTimeout(r, 2000));
        job = await api(`/api/v1/runs/jobs/${job.job_id}`);
        if (job.progress && STEPS.includes(job.progress)) showProgress(job.progress);
      }
      if (job.status === "failed") throw new Error(job.error || "The collection failed.");
      showProgress("index", true);
      toast("Live collection finished");
      await Promise.all([loadRuns(), loadAiOps(), loadAudit()]);
    } catch (e) {
      $("run-error").textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  let EDITING_DEFAULT = true;

  function useProduct(id) {
    document.cookie = `fz_profile=${encodeURIComponent(id)}; path=/; max-age=31536000; samesite=lax`;
    location.reload();
  }

  function showProducts(items) {
    $("profiles-rows").replaceChildren(...items.map((x) => el("tr", { "data-testid": "profile-row" },
      el("td", {}, x.name, x.is_default ? el("span", { class: "badge" }, " default") : null, x.current ? el("span", { class: "badge" }, " viewing") : null),
      el("td", {}, x.focus_brand),
      el("td", {}, x.last_run_at ? fmt(x.last_run_at) : "Not collected yet"),
      el("td", { class: "row-actions" },
        x.current ? null : el("button", { type: "button", class: "btn ghost small", onclick: () => useProduct(x.id) }, "View"),
        x.is_default ? null : el("button", { type: "button", class: "btn ghost small", "data-testid": "make-default",
          onclick: () => api(`/api/v1/admin/profiles/${x.id}/default`, { method: "POST" }).then(loadProfile).then(() => toast(`${x.name} is now the default`)) }, "Make default"),
        x.is_default ? null : el("button", { type: "button", class: "btn ghost small", "data-testid": "delete-profile",
          onclick: () => { if (confirm(`Stop tracking ${x.name}? Its past collections stay in the history.`))
            api(`/api/v1/admin/profiles/${x.id}`, { method: "DELETE" }).then(() => (x.current ? useProduct("") : loadProfile())); } }, "Remove")))));
  }

  function showProfile(data) {
    const p = data.active;
    $("onboarding").hidden = !!p;
    showProducts(data.profiles || []);
    const cur = (data.profiles || []).find((x) => x.current);
    EDITING_DEFAULT = !cur || cur.is_default;
    $("editing-name").textContent = p ? `${p.name}${EDITING_DEFAULT ? " (default)" : ""}` : "nothing yet";
    HZ.profileSwitcher($("profile-switch"));
    $("preset").replaceChildren(...data.presets.map((x) => el("option", { value: x.id }, `${x.name} (${x.focus_brand} vs ${x.rivals.length} rivals)`)));
    if (!p) {
      $("track-current").replaceChildren(el("dt", {}, "Setup"), el("dd", {}, "Nothing tracked yet"));
      $("profile-json").value = "";
      $("collect-note").textContent = "Choose what to track first.";
      $("run-youtube").disabled = true;
      return;
    }
    $("track-current").replaceChildren(...[
      ["Setup", p.name],
      ["Your brand", p.focus_brand],
      ["Rivals", Object.keys(p.brands).filter((b) => b !== p.focus_brand).join(", ")],
      ["Search terms", p.keywords.join(", ")],
      ["Last changed", data.updated_by ? `${fmt(data.updated_at)} by ${data.updated_by}` : "Set by the server configuration"],
    ].flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    if (data.presets.some((x) => x.id === p.id)) $("preset").value = p.id;
    $("profile-json").value = JSON.stringify(p, null, 2);
    const units = 151 * p.keywords.length;
    $("collect-note").textContent = `A live collection reads ${p.keywords.length} search terms, 50 videos each, and uses about ${units.toLocaleString("en-IN")} of the 10,000 free daily API units. It runs as a LangGraph agent with checkpoints, so a failed run can resume.`;
  }

  const usd = (x) => (x == null ? "–" : `$${Number(x).toFixed(x < 0.01 ? 5 : 3)}`);
  const pctOrDash = (x) => (x == null ? "–" : `${(100 * x).toFixed(1)}%`);

  async function loadAiOps() {
    const o = await api("/api/v1/admin/ai-ops");
    const facts = [
      [String(o.calls.total), "LLM calls (7 days)"],
      [o.tokens.prompt.toLocaleString("en-IN") + " / " + o.tokens.completion.toLocaleString("en-IN"), "Prompt / completion tokens"],
      [o.tokens.cached_prompt.toLocaleString("en-IN"), "Provider-cached prompt tokens"],
      [usd(o.cost_usd.total), "Spend"],
      [pctOrDash(o.cache_hit_rate), "Response-cache hits"],
      [pctOrDash(o.schema_valid_rate), "Replies matching the schema"],
      [o.tokens.estimate_error_pct == null ? "–" : `${o.tokens.estimate_error_pct}%`, "Token estimate error"],
      [String(o.calls.refused), "Refused by policy"],
    ];
    if (o.rag && o.rag.run_id) {
      facts.push([`${o.rag.embedded.toLocaleString("en-IN")} / ${o.rag.chunks.toLocaleString("en-IN")}`,
        `Comments searchable by meaning (${o.rag.embed_model}, ${o.rag.embed_dims} dims)`]);
    }
    $("aiops-facts").replaceChildren(...facts.map(([v, k]) => el("div", { class: "fact" }, el("div", { class: "v small" }, v), el("div", { class: "k" }, k))));
    $("aiops-nodes").replaceChildren(...Object.entries(o.by_node).map(([n, d]) => el("tr", {},
      el("td", {}, n), el("td", { class: "num" }, String(d.calls)), el("td", { class: "num" }, String(d.cached)),
      el("td", { class: "num" }, String(d.refused)), el("td", { class: "num" }, String(d.prompt_tokens)),
      el("td", { class: "num" }, String(d.est_prompt_tokens)), el("td", { class: "num" }, String(d.completion_tokens)),
      el("td", { class: "num" }, usd(d.cost_usd)), el("td", { class: "num" }, String(d.p95_ms)))));
    if (!Object.keys(o.by_node).length) $("aiops-nodes").replaceChildren(el("tr", {}, el("td", { colspan: "9" }, "No model calls yet: the agents ran on rules only.")));
    $("aiops-spans").replaceChildren(...Object.entries(o.spans).map(([n, d]) => el("tr", {},
      el("td", {}, n), el("td", { class: "num" }, String(d.count)), el("td", { class: "num" }, String(d.p50_ms)), el("td", { class: "num" }, String(d.p95_ms)))));
    $("aiops-prompts").replaceChildren(...o.prompts.map((p) => el("li", {}, el("strong", {}, `${p.id}@${p.version}`), ` (${p.sha}) ${p.description}`)));
    const g = o.governance;
    $("gov").replaceChildren(...[
      ["Model", `${g.model} (${g.model_allowed ? "allowed" : "NOT on the allow-list"})`],
      ["Language model", g.llm_configured ? (g.llm_enabled ? "On" : "Off by kill switch") : "Not configured: rules only"],
      ["Tasks a model may do", g.tasks.join(", ")],
      ["Never", g.llm_may_not.join("; ")],
      ["Budgets", `$${g.budgets.per_run_usd} per run, $${g.budgets.per_day_usd} per day, ${g.budgets.max_prompt_tokens.toLocaleString("en-IN")} prompt tokens; spent today ${usd(g.budgets.spent_today_usd)}`],
    ].flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  }

  async function loadAudit() {
    const rows = await api("/api/v1/admin/audit?limit=15");
    $("audit").replaceChildren(...rows.map((r) => el("tr", { "data-testid": "audit-row" },
      el("td", {}, fmt(r.ts)), el("td", {}, r.actor), el("td", {}, r.action), el("td", {}, r.target || ""))));
  }

  async function loadProfile() {
    showProfile(await api("/api/v1/admin/profile"));
  }

  async function saveProfile(body, done) {
    $("profile-error").textContent = "";
    try {
      showProfile(await api("/api/v1/admin/profile", { method: "PUT", body: JSON.stringify(body) }));
      toast(done);
    } catch (e) {
      $("profile-error").textContent = e.message;
    }
  }

  function saveCustom() {
    let profile;
    try {
      profile = JSON.parse($("profile-json").value);
    } catch {
      $("profile-error").textContent = "The profile is not valid JSON. Check for a missing comma or quote.";
      return;
    }
    return profile;
  }

  function saveEdited() {
    const profile = saveCustom();
    if (profile) saveProfile({ profile, make_default: EDITING_DEFAULT }, `Saved ${profile.name}`);
  }

  function saveAsNew() {
    const profile = saveCustom();
    if (profile) saveProfile({ profile, make_default: false }, `Now also tracking ${profile.focus_brand}. Collect live data to see it.`);
  }

  function userRow(u) {
    const role = el("select", { class: "input", "aria-label": `Role for ${u.name}`, "data-testid": `role-${u.email}`, disabled: u.id === me.id },
      el("option", { value: "analyst" }, "Analyst"), el("option", { value: "admin" }, "Admin"));
    role.value = u.role;
    role.addEventListener("change", () => patch(u, { role: role.value }, `${u.name} is now ${role.value === "admin" ? "an admin" : "an analyst"}`));
    const access = el("button", { class: "btn ghost small", type: "button", "data-testid": `access-${u.email}`, disabled: u.id === me.id,
      onclick: () => patch(u, { active: !u.active }, u.active ? `Removed access for ${u.name}` : `Restored access for ${u.name}`) },
      u.active ? "Remove access" : "Restore access");
    const reset = el("button", { class: "btn ghost small", type: "button", "data-testid": `reset-${u.email}`, onclick: () => resetPassword(u) }, "Set new password");
    return el("tr", { "data-testid": "user-row" }, el("td", {}, u.name), el("td", {}, u.email), el("td", {}, role),
      el("td", {}, u.active ? "Active" : "Removed", " ", access), el("td", {}, fmt(u.last_login)), el("td", {}, reset));
  }

  async function patch(u, body, done) {
    try {
      await api(`/api/v1/admin/users/${u.id}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(done);
    } catch (e) {
      toast(e.message);
    }
    loadUsers();
  }

  function resetPassword(u) {
    const pw = window.prompt(`New password for ${u.name} (at least 10 characters). Share it with them privately; they can sign in with it right away.`);
    if (pw) patch(u, { password: pw }, `Password changed for ${u.name}`);
  }

  async function loadUsers() {
    const users = await api("/api/v1/admin/users");
    $("users").replaceChildren(...users.map(userRow));
  }

  async function loadSystem() {
    const s = await api("/api/v1/admin/system");
    const rows = [
      ["YouTube API key", s.youtube_key_set ? "Set" : "Not set: add FANVAAZ_YOUTUBE_API_KEY to collect live data"],
      ["Database", s.database],
      ["Language model", s.llm_configured ? `On (${s.llm_model}; embeddings ${s.embed_model})` : "Off: rules only"],
      ["MCP server", s.mcp.http ? `On at /mcp (${s.mcp.read_token_set ? "read token set" : "admin token only"})` : "Off"],
      ["Daily collection", s.daily_run_at ? `Every day at ${s.daily_run_at} (${s.timezone})` : "Cloud Scheduler (or set FANVAAZ_DAILY_RUN_AT, for example 06:30)"],
      ["Who can sign up", s.signup_domains.length ? s.signup_domains.map((d) => "@" + d).join(", ") : "Anyone with the link"],
      ["People", String(s.users)],
      ["Version", s.version],
    ];
    $("system").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    const yt = $("run-youtube");
    yt.disabled = !s.youtube_key_set;
    $("yt-hint").hidden = s.youtube_key_set;
  }

  async function init() {
    $("signout").addEventListener("click", HZ.signOut);
    $("run-youtube").addEventListener("click", (e) => startRun(e.currentTarget));
    $("use-preset").addEventListener("click", () => saveProfile({ preset: $("preset").value }, "Setup switched. Collect live data to see it."));
    $("save-profile").addEventListener("click", saveEdited);
    $("add-profile").addEventListener("click", saveAsNew);
    $("add-preset").addEventListener("click", () => saveProfile({ preset: $("preset").value, make_default: false }, "Product added. Collect live data to see it."));
    me = await api("/api/v1/auth/me");
    $("user-name").textContent = `Signed in as ${me.name}`;
    await Promise.all([loadRuns(), loadUsers(), loadSystem(), loadAiOps(), loadAudit()]);
    await loadProfile();
    document.body.dataset.ready = "true";
  }
  init();
})();
