"use strict";
// Illustrative only: the real shares are behind sign-in.
const EXAMPLE = [
  { brand: "Atomberg", sov: 0.29 }, { brand: "Havells", sov: 0.18 }, { brand: "Crompton", sov: 0.17 },
  { brand: "Orient", sov: 0.10 }, { brand: "Usha", sov: 0.07 }, { brand: "Bajaj", sov: 0.06 },
  { brand: "Superfan", sov: 0.06 }, { brand: "Polycab", sov: 0.04 },
];
const fig = document.getElementById("hero-fan");
HZ.fan(fig, EXAMPLE, { labels: true, title: "Example share of voice fan", focus: "Atomberg" });
fig.append(HZ.el("figcaption", { class: "example" }, "Each blade is a brand, sized by its share of voice. Example: Atomberg among ceiling-fan brands."));
