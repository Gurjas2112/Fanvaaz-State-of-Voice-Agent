"""Write docs/TWO_PAGER.md (the problem statement's submission) from the latest run.

Page 1: tech stack, tools, links, quality results (read from reports/*.json).
Page 2: findings and recommendations (read from the latest stored run).

If the latest run is the synthetic demo, page 2 is stamped as a template so it
cannot be submitted by mistake. Run `hawaaz run --source youtube` first.
"""

from __future__ import annotations

import json
from pathlib import Path

from hawaaz.config import DEFAULT_KEYWORDS, FOCUS_BRAND, get_settings
from hawaaz.store import Store

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
OUT = ROOT / "docs" / "TWO_PAGER.md"
REPO_URL = "https://github.com/Gurjas2112/Hawaaz-SoV-Agent"  # update after pushing
DEMO_URL = "<add your deployed URL>"


def _load(name: str) -> dict | None:
    p = REPORTS / name
    return json.loads(p.read_text()) if p.exists() else None


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def quality_rows() -> list[str]:
    rows = []
    g = _load("golden_holdout.json")
    if g:
        rows.append(
            f"| Brand detection (held-out golden set, {g['brand_pairs']} pairs) | F1 {g['brand']['f1']:.2f} |"
        )
        rows.append(
            f"| Brand-targeted sentiment, rules only (held-out) | macro-F1 {g['sentiment']['macro_f1']:.2f} |"
        )
    s = _load("safety.json")
    if s:
        rows.append(f"| Red-team questions blocked | {s['blocked']}/{s['attacks']} |")
        rows.append(f"| Benign questions wrongly refused | {s['false_refusals']}/{s['benign']} |")
    pf = _load("promptfoo.json")
    if pf:
        st = pf.get("results", {}).get("stats", {})
        if st:
            rows.append(
                f"| promptfoo behaviour + refusal checks | {st.get('successes', 0)}/"
                f"{st.get('successes', 0) + st.get('failures', 0)} pass |"
            )
    k6 = _load("k6_summary.json")
    if k6:
        d = k6["metrics"].get("http_req_duration", {})
        rows.append(f"| API p95 latency under k6 load (55 req/s) | {d.get('p(95)', 0):.1f} ms |")
    de = _load("deepeval.json")
    if de:
        f = [r["scores"]["FaithfulnessMetric"]["score"] for r in de["results"]]
        rows.append(f"| DeepEval faithfulness (judge {de['judge']}) | {sum(f) / len(f):.2f} |")
    return rows


def main() -> None:
    s = get_settings()
    st = Store(s.db_path)
    run = st.latest_run()
    if not run:
        raise SystemExit("No runs yet. hawaaz run --source youtube")
    reports = {k: st.report(run.run_id, k) for k in st.keywords(run.run_id)}
    insights = st.insights(run.run_id)
    demo = run.source == "synthetic-demo"

    lines = [
        "# Hawaaz: Atomberg's Share of Voice on YouTube",
        "",
        "## Page 1: How it works",
        "",
        "**AI agent:** searches YouTube for each keyword, reads the top N = 50 videos and up to 100 top comments each, "
        "finds brand mentions, scores brand-targeted sentiment, computes Share of Voice and writes recommendations. "
        "A guarded Q&A lets the team ask follow-up questions.",
        "",
        f"**Keywords ({len(DEFAULT_KEYWORDS)}):** " + ", ".join(DEFAULT_KEYWORDS),
        "",
        "**Why N = 50:** one YouTube search page returns 50 results; few viewers scroll past it, and the rank "
        "weight below already makes result 50 count 18% of result 1. Cost: ~151 API units per keyword, ~906 of the "
        "10,000 free daily units for all six.",
        "",
        "**Share of Voice metric** (per keyword, result at rank r weighted w = 1/log2(r+1)):",
        "",
        "- M = share of rank-weighted mentions (title counts 2, description 1, each comment 1)",
        "- E = share of rank-weighted engagement, ln(1+views) + 2 ln(1+likes) + 3 ln(1+comments), split among "
        "brands in the video",
        "- P = Share of Positive Voice: share of rank-weighted positive mentions",
        "- **SoV = 0.3 M + 0.4 E + 0.3 P** (weights configurable; all three reported separately)",
        "",
        "| Layer | Tools |",
        "| --- | --- |",
        "| Collection | YouTube Data API v3 (httpx, retries, quota accounting, raw responses saved for replay) |",
        "| NLP | Rule-based brand matcher (aliases, Hindi script); brand-targeted sentiment for English + Hinglish "
        "with comparatives, negation and contrast; optional LLM relabelling (any OpenAI-compatible model) |",
        "| Risk radar | Safety, legal/consumer, product and service-failure flags per brand, scored by weight and "
        "likes, with evidence; CSV export for Tableau / Power BI (adapted from our CRIF hackathon project) |",
        "| Safety | PII redaction at ingestion; prompt-injection quarantine for comments; question guardrails; "
        "numeric grounding check on every LLM output |",
        "| Product | Landing, sign-up/sign-in with roles, daily brief with change since the last run, reply inbox, "
        "admin panel; FastAPI (OpenAPI), SQLite, Docker |",
        "| Quality | pytest + Hypothesis, golden sets, DeepEval, Ragas, promptfoo, Postman/Newman, Schemathesis, "
        "Playwright, Appium, k6, GitHub Actions |",
        "",
        f"**Links:** code {REPO_URL} · live demo {DEMO_URL}",
        "",
    ]
    q = quality_rows()
    if q:
        lines += ["**Measured quality**", "", "| Check | Result |", "| --- | --- |", *q, ""]

    lines += ["## Page 2: Findings and recommendations", ""]
    if demo:
        lines += [
            "> **TEMPLATE ONLY: the numbers below come from the synthetic demo dataset, not YouTube.** "
            "Run `hawaaz run --source youtube` and re-run this script before submitting.",
            "",
        ]
    lines += [
        f"Run `{run.run_id}` · {run.videos} result slots · {run.comments} comments · source: {run.source}",
        "",
    ]

    lines += [
        "| Keyword | Atomberg SoV | Rank | Leader (SoV) | Atomberg positive share |",
        "| --- | --- | --- | --- | --- |",
    ]
    for k, r in reports.items():
        names = [b.brand for b in r.brands]
        me = next((b for b in r.brands if b.brand == FOCUS_BRAND), None)
        lead = r.brands[0] if r.brands else None
        lines.append(
            f"| {'All keywords' if k == 'all' else k} | {pct(me.sov) if me else '0%'} | "
            f"{names.index(FOCUS_BRAND) + 1 if me else '-'} of {len(names)} | "
            f"{lead.brand} ({pct(lead.sov)}) | {pct(me.positive_share) if me else '-'} |"
        )
    lines += ["", "**Insights and recommendations**", ""]
    for i, ins in enumerate(insights, 1):
        lines.append(f"{i}. **{ins['title']}.** {ins['text']}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({'TEMPLATE, demo data' if demo else 'live data'})")


if __name__ == "__main__":
    main()
