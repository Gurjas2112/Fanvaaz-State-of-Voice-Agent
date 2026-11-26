"use strict";
// Shared helpers: API calls (with the CSRF header), safe DOM building, the fan chart.
const HZ = (() => {
  // The brand this workspace reports on; pages set it from the API (any brand, not one company).
  const state = { focus: "" };
  const setFocus = (brand) => { state.focus = brand || ""; };

  async function api(path, opts = {}) {
    const headers = { "X-Requested-With": "fanvaaz", ...(opts.body ? { "Content-Type": "application/json" } : {}) };
    const r = await fetch(path, { credentials: "same-origin", ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    if (r.status === 401 && !path.includes("/auth/")) {
      location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      throw new Error("Signed out");
    }
    const ct = r.headers.get("content-type") || "";
    const data = r.status === 204 ? null : ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : data.detail[0]?.msg?.replace(/^Value error, /, ""))) || `Request failed (${r.status})`);
    return data;
  }

  // Build elements with textContent only: comment text is untrusted.
  function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v == null) continue;
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }

  const pct = (x, d = 1) => `${(100 * x).toFixed(d)}%`;
  const $ = (id) => document.getElementById(id);

  let toastTimer = null;
  function toast(msg) {
    // One toast at a time: a new message replaces the previous one instead of stacking on top of it.
    document.querySelectorAll(".toast").forEach((n) => n.remove());
    clearTimeout(toastTimer);
    const t = el("div", { class: "toast", role: "status" }, msg);
    document.body.append(t);
    toastTimer = setTimeout(() => t.remove(), 2600);
  }

  // The share-of-voice fan: one blade per brand, length by share, the focus brand in teal.
  const SVGNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs = {}) {
    const e = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }
  // A swept blade pointing up: the trailing edge bulges and the tip leans clockwise, so the fan reads as turning.
  function blade(len, width, r0 = 30) {
    const tip = r0 + len, root = Math.max(3, r0 * 0.23);
    return `M ${-root} ${-r0} C ${-width} ${-(r0 + len * 0.3)}, ${-width * 0.45} ${-(r0 + len * 0.92)}, ${width * 0.35} ${-tip}` +
           ` C ${width * 1.15} ${-(r0 + len * 0.78)}, ${width * 0.95} ${-(r0 + len * 0.22)}, ${root} ${-r0} Z`;
  }
  function fan(container, brands, { labels = true, maxBlades = 8, title = "Share of voice by brand", focus = state.focus } = {}) {
    const FOCUS = focus;
    const rows = brands.slice(0, maxBlades);
    const max = Math.max(...rows.map((b) => b.sov), 0.0001);
    const R = 150, W = labels ? 300 : 230, H = labels ? 262 : 230;
    const root = svg("svg", { viewBox: `${-W} ${-H} ${2 * W} ${2 * H}`, role: "img", "aria-label": title });
    const desc = svg("title"); desc.textContent = title + ": " + rows.map((b) => `${b.brand} ${pct(b.sov)}`).join(", ");
    root.append(desc);
    const spin = svg("g", { class: "spin" });
    const step = 360 / rows.length;
    rows.forEach((b, i) => {
      const len = 26 + R * Math.sqrt(b.sov / max); // area-honest: length grows with the square root
      const p = svg("path", { d: blade(len, 15 + 22 * (b.sov / max)), class: "blade" + (b.brand === FOCUS ? " focus" : ""),
        transform: `rotate(${i * step})`, "data-brand": b.brand });
      if (b.brand !== FOCUS) p.setAttribute("opacity", String(0.45 + 0.55 * (b.sov / max)));
      spin.append(p);
    });
    root.append(spin, svg("circle", { r: 34, class: "hub-ring" }), svg("circle", { r: 26, class: "hub" }));
    if (labels) {
      rows.forEach((b, i) => {
        const a = (i * step - 90) * Math.PI / 180;
        const tip = 30 + 26 + R * Math.sqrt(b.sov / max);
        const x = Math.cos(a) * (tip + 18), y = Math.sin(a) * (tip + 18) + (Math.sin(a) < -0.9 ? -4 : 0);
        const t = svg("text", { x: x.toFixed(1), y: (y + 4).toFixed(1), "text-anchor": Math.abs(x) < 12 ? "middle" : x > 0 ? "start" : "end",
          class: "blade-label" + (b.brand === FOCUS ? " focus" : "") });
        t.textContent = b.brand === FOCUS ? `${b.brand} ${pct(b.sov, 0)}` : b.brand;
        root.append(t);
      });
    }
    container.replaceChildren(root);
  }

  // Small mark used in the logo: three blades.
  function mark() {
    const root = svg("svg", { viewBox: "-50 -50 100 100", "aria-hidden": "true" });
    [0, 120, 240].forEach((r, i) => root.append(svg("path", { d: blade(30, 15, 12), transform: `rotate(${r})`,
      fill: i === 0 ? "#0f8b8d" : "currentColor" })));
    root.append(svg("circle", { r: 11, fill: "currentColor" }));
    return root;
  }
  document.querySelectorAll("[data-mark]").forEach((n) => n.prepend(mark()));

  async function signOut() {
    await api("/api/v1/auth/logout", { method: "POST" });
    location.href = "/";
  }

  // Product switcher: several tracked products, one chosen per browser (cookie read by the server).
  async function profileSwitcher(select) {
    if (!select) return [];
    let items = [];
    try { items = await api("/api/v1/profiles"); } catch { return []; }
    select.hidden = items.length < 2;
    select.replaceChildren(...items.map((p) => el("option", { value: p.id, selected: p.current || null },
      `${p.name}${p.is_default ? " (default)" : ""}`)));
    select.onchange = () => {
      document.cookie = `fz_profile=${encodeURIComponent(select.value)}; path=/; max-age=31536000; samesite=lax`;
      location.reload();
    };
    return items;
  }

  return { api, el, pct, $, toast, fan, signOut, setFocus, state, profileSwitcher };
})();
