"use strict";
(() => {
  const side = document.getElementById("side-fan");
  if (side) {
    HZ.fan(side, [0.29, 0.18, 0.17, 0.1, 0.07, 0.06].map((sov, i) => ({ brand: i ? `b${i}` : "you", sov })),
      { labels: false, title: "Fanvaaz", focus: "you" });
  }
  const next = new URLSearchParams(location.search).get("next");
  const safeNext = next && next.startsWith("/") && !next.startsWith("//") ? next : "/app";

  function wire(formId, path, fields) {
    const form = document.getElementById(formId);
    if (!form) return;
    const err = document.getElementById("form-error");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      const body = Object.fromEntries(fields.map((f) => [f, form.elements[f].value.trim()]));
      body.password = form.elements.password.value; // never trim passwords
      if (fields.includes("name") && !body.name) return (err.textContent = "Enter your name.");
      if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(body.email)) return (err.textContent = "Enter a valid email address.");
      if (formId === "signup-form" && body.password.length < 10) return (err.textContent = "Use at least 10 characters for the password.");
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        await HZ.api(path, { method: "POST", body: JSON.stringify(body) });
        location.href = safeNext;
      } catch (ex) {
        err.textContent = ex.message;
        btn.disabled = false;
      }
    });
  }
  wire("login-form", "/api/v1/auth/login", ["email", "password"]);
  wire("signup-form", "/api/v1/auth/signup", ["name", "email", "password"]);

  const pw = document.getElementById("password-form");
  if (pw) {
    const err = document.getElementById("form-error");
    const ok = document.getElementById("form-ok");
    HZ.api("/api/v1/auth/me").then((u) => {
      document.getElementById("account-email").textContent = `Signed in as ${u.email}`;
    });
    pw.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = ok.textContent = "";
      const cur = pw.elements.current_password.value;
      const nw = pw.elements.new_password.value;
      if (!cur) return (err.textContent = "Enter your current password.");
      if (nw.length < 10) return (err.textContent = "Use at least 10 characters for the new password.");
      if (nw !== pw.elements.confirm_password.value) return (err.textContent = "The new passwords do not match.");
      const btn = pw.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        await HZ.api("/api/v1/auth/password", {
          method: "POST",
          body: JSON.stringify({ current_password: cur, new_password: nw }),
        });
        pw.reset();
        ok.textContent = "Password changed. Other sessions were signed out.";
      } catch (ex) {
        err.textContent = ex.message;
      }
      btn.disabled = false;
    });
  }
})();
