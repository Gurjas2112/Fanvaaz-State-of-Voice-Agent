"""Write docs/RUN_REPORT.md from the latest stored run and the harness reports.

Section 1: what was collected and how Share of Voice is computed.
Section 2: findings and recommendations, every one with its evidence links.
Section 3: measured quality (read from reports/*.json written by the test harness).

Run a live collection first (`fanvaaz run`), then `make report`.
"""

from __future__ import annotations

import json
from pathlib import Path

from fanvaaz.config import get_settings
from fanvaaz.store import Store

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
OUT = ROOT / "docs" / "RUN_REPORT.md"


def _load(name: str) -> dict | None:
    p = REPORTS / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def quality_rows() -> list[str]:
    rows = []
    for split, label in (
        ("holdout", "hand-written holdout"),
        ("real_holdout", "REAL YouTube comments, holdout"),
    ):
        g = _load(f"golden_{split}.json")
        if g:
            rows.append(
                f"| Sentiment macro-F1, rules only ({label}, {g['brand_pairs']} pairs) | "
                f"{g['sentiment']['macro_f1']:.2f} |"
            )
    g = _load("golden_holdout.json")
    if g:
        rows.append(f"| Brand detection F1 (hand-written holdout) | {g['brand']['f1']:.2f} |")
    s = _load("safety.json")
    if s:
        rows.append(f"| Red-team questions blocked | {s['blocked']}/{s['attacks']} |")
        rows.append(f"| Benign questions wrongly refused | {s['false_refusals']}/{s['benign']} |")
    q = _load("real_quarantine.json")
    if q:
        rows.append(
            f"| Real comments wrongly quarantined as injections | {q['quarantined']}/{q['comments']} |"
        )
    de = _load("deepeval.json")
    if de:
        f = [r["scores"]["FaithfulnessMetric"]["score"] for r in de["results"]]
        rows.append(f"| DeepEval faithfulness (judge {de['judge']}) | {sum(f) / len(f):.2f} |")
    k6 = _load("k6_summary.json")
    if k6:
        d = k6["metrics"].get("http_req_duration", {})
        rows.append(f"| API p95 latency under k6 load | {d.get('p(95)', 0):.1f} ms |")
    return rows


def main() -> None:
    s = get_settings()
    st = Store.from_settings(s)
    run = st.latest_run()
    if not run:
        raise SystemExit("No runs yet. Collect live data first: fanvaaz run")
    reports = {k: st.report(run.run_id, k) for k in st.keywords(run.run_id)}
    insights = st.insights(run.run_id)
    focus = run.focus_brand
    lines = [
        f"# Fanvaaz run report: {focus} on YouTube",
        "",
        f"Run `{run.run_id}` collected {run.created_at} · source: {run.source} (YouTube Data API v3) · "
        f"{run.videos} result slots · {run.comments:,} comments · {run.quota_units_used} quota units",
        "",
        f"**Keywords ({len(run.keywords)}):** " + ", ".join(run.keywords),
        "",
        "**Share of Voice** (per keyword; a result at rank r is weighted w = 1/log2(r+1)):",
        "M = share of rank-weighted mentions, E = share of rank-weighted engagement, "
        f"P = share of rank-weighted positive mentions; **SoV = {s.w_mentions} M + {s.w_engagement} E + "
        f"{s.w_positive} P**.",
        "",
        "## Share of Voice by keyword",
        "",
        f"| Keyword | {focus} SoV | Rank | Leader (SoV) | {focus} positive share |",
        "| --- | --- | --- | --- | --- |",
    ]
    for k, r in reports.items():
        names = [b.brand for b in r.brands]
        me = next((b for b in r.brands if b.brand == focus), None)
        lead = r.brands[0] if r.brands else None
        lines.append(
            f"| {'All keywords' if k == 'all' else k} | {pct(me.sov) if me else '0%'} | "
            f"{names.index(focus) + 1 if me else '-'} of {len(names)} | "
            f"{lead.brand} ({pct(lead.sov)}) | {pct(me.positive_share) if me else '-'} |"
        )
    lines += ["", "## Insights and recommendations", ""]
    for i, ins in enumerate(insights, 1):
        cites = " ".join(f"[{n}]({c['url']})" for n, c in enumerate(ins.get("citations", [])[:3], 1))
        check = " (verified)" if ins.get("verified") else ""
        lines.append(f"{i}. **{ins['title']}.** {ins['text']}{check} {cites}".rstrip())
    q = quality_rows()
    if q:
        lines += ["", "## Measured quality", "", "| Check | Result |", "| --- | --- |", *q]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
