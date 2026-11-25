"use strict";
// The hero chart is live: the latest real collection's overall share of voice
// (aggregates only, from /api/v1/public/preview). No invented numbers: with no
// collection yet, the page says so instead of drawing an example.
(async () => {
  const fig = document.getElementById("hero-fan");
  try {
    const r = await fetch("/api/v1/public/preview", { headers: { Accept: "application/json" } });
    const p = await r.json();
    if (!p.available || !p.brands.length) {
      fig.append(HZ.el("figcaption", { class: "example", "data-testid": "preview-empty" },
        "The chart appears after the first live YouTube collection."));
      return;
    }
    HZ.fan(fig, p.brands, { labels: true, title: "Latest share of voice", focus: p.focus_brand });
    const when = new Date(p.collected_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
    fig.append(HZ.el("figcaption", { class: "example", "data-testid": "preview-caption" },
      `Live: ${p.focus_brand} and rivals across ${p.videos} YouTube results and ` +
      `${p.comments.toLocaleString("en-IN")} comments, collected ${when}. Each blade is a brand, sized by its share of voice.`));
  } catch {
    fig.append(HZ.el("figcaption", { class: "example" }, "Share of voice preview is unavailable right now."));
  }
})();
