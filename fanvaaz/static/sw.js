/* Fanvaaz service worker: caches the app shell only (styles, scripts, fonts, icons).
   API responses and pages are always fetched from the network, so data is never stale;
   offline, the shell explains that a connection is needed. */
"use strict";
const SHELL = "fanvaaz-shell-v2";
const ASSETS = [
  "/static/hz.css", "/static/hz.js", "/static/app.js", "/static/admin.js", "/static/auth.js",
  "/static/landing.js", "/static/pwa.js", "/static/icons/icon-192.png",
  "/static/fonts/bricolage-grotesque-latin-wght-normal.woff2",
  "/static/fonts/atkinson-hyperlegible-latin-400-normal.woff2",
  "/static/fonts/atkinson-hyperlegible-latin-700-normal.woff2",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => new Response(
      "<!doctype html><meta name=viewport content='width=device-width'><title>Fanvaaz</title>" +
      "<body style='font-family:system-ui;padding:2rem'><h1>You are offline</h1>" +
      "<p>Fanvaaz shows live data, so it needs a connection. Try again when you are back online.</p>",
      { headers: { "Content-Type": "text/html; charset=utf-8" } })));
  }
});
