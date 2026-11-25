"use strict";
(() => {
  const { api, el, $, toast } = HZ;
  let me = null;

  const fmt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Never");

  async function loadRuns() {
    const runs = await api("/api/v1/admin/runs");
    $("runs").replaceChildren(...runs.map((r) => el("tr", { "data-testid": "run-row" },
      el("td", {}, fmt(r.created_at)), el("td", {}, r.focus_brand), el("td", {}, r.source === "youtube" ? "YouTube" : "Demo data"),
      el("td", { class: "num" }, String(r.videos)), el("td", { class: "num" }, r.comments.toLocaleString("en-IN")),
      el("td", { class: "num" }, String(r.quota_units_used)))));
  }

  async function startRun(source, btn) {
    $("run-error").textContent = "";
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = source === "youtube" ? "Collecting, this takes a few minutes…" : "Loading…";
    try {
      await api("/api/v1/runs", { method: "POST", body: JSON.stringify({ source }) });
      toast(source === "youtube" ? "Collection finished" : "Demo data loaded");
      await loadRuns();
    } catch (e) {
      $("run-error").textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  function showProfile(data) {
    const p = data.active;
    $("track-current").replaceChildren(...[
      ["Setup", p.name],
      ["Your brand", p.focus_brand],
      ["Rivals", Object.keys(p.brands).filter((b) => b !== p.focus_brand).join(", ")],
      ["Search terms", p.keywords.join(", ")],
      ["Last changed", data.updated_by ? `${fmt(data.updated_at)} by ${data.updated_by}` : "Default setup"],
    ].flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    $("preset").replaceChildren(...data.presets.map((x) => el("option", { value: x.id }, `${x.name} (${x.focus_brand} vs ${x.rivals.length} rivals)`)));
    if (data.presets.some((x) => x.id === p.id)) $("preset").value = p.id;
    $("profile-json").value = JSON.stringify(p, null, 2);
    const units = 151 * p.keywords.length;
    $("collect-note").textContent = `A YouTube collection reads ${p.keywords.length} search terms, 50 videos each, and uses about ${units.toLocaleString("en-IN")} of the 10,000 free daily API units.`
      + (p.demo_data ? "" : " This setup has no demo data, so collect from YouTube.");
    $("run-demo").disabled = !p.demo_data;
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
    saveProfile({ profile }, `Now tracking ${profile.focus_brand}`);
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
      ["Language model", s.llm_configured ? `On (${s.llm_model})` : "Off: rules only"],
      ["Daily collection", s.daily_run_at ? `Every day at ${s.daily_run_at} (${s.timezone})` : "Off: set FANVAAZ_DAILY_RUN_AT, for example 06:30"],
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
    $("run-youtube").addEventListener("click", (e) => startRun("youtube", e.currentTarget));
    $("run-demo").addEventListener("click", (e) => startRun("demo", e.currentTarget));
    $("use-preset").addEventListener("click", () => saveProfile({ preset: $("preset").value }, "Setup switched. Collect data to see it."));
    $("save-profile").addEventListener("click", saveCustom);
    me = await api("/api/v1/auth/me");
    $("user-name").textContent = `Signed in as ${me.name}`;
    await Promise.all([loadRuns(), loadUsers(), loadSystem()]);
    await loadProfile();
    document.body.dataset.ready = "true";
  }
  init();
})();
