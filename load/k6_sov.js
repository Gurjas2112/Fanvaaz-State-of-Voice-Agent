// k6 load test: read endpoints at 50 requests/s for 1 minute, plus a trickle of /ask.
//   k6 run -e BASE_URL=http://127.0.0.1:8000 load/k6_sov.js
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const AUTH = { Authorization: `Bearer ${__ENV.HAWAAZ_API_TOKEN || "local-service-token"}` };
const KEYWORDS = ["all", "smart fan", "BLDC fan", "energy saving fan"];

export const options = {
  scenarios: {
    reads: { executor: "constant-arrival-rate", rate: 50, timeUnit: "1s", duration: "1m",
             preAllocatedVUs: 20, maxVUs: 100, exec: "reads" },
    asks: { executor: "constant-arrival-rate", rate: 5, timeUnit: "1s", duration: "1m",
            preAllocatedVUs: 5, maxVUs: 20, exec: "asks" },
  },
  thresholds: {
    "http_req_failed": ["rate<0.01"],
    "http_req_duration{scenario:reads}": ["p(95)<500"],
    "http_req_duration{scenario:asks}": ["p(95)<1500"],
  },
};

export function reads() {
  const kw = KEYWORDS[Math.floor(Math.random() * KEYWORDS.length)];
  const r = http.get(`${BASE}/api/v1/sov?keyword=${encodeURIComponent(kw)}`, { headers: AUTH });
  check(r, { "200": (res) => res.status === 200, "has brands": (res) => res.json("report.brands").length > 0 });
}

export function asks() {
  const r = http.post(`${BASE}/api/v1/ask`, JSON.stringify({ question: "Why do people complain about Atomberg?" }),
    { headers: { "Content-Type": "application/json", ...AUTH } });
  check(r, { "200": (res) => res.status === 200, "not refused": (res) => res.json("refused") === false });
}
