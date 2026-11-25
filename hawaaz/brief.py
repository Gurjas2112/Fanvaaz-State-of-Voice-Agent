"""The morning brief: what changed since the previous run, in one sentence.

Built for daily use: a marketer opens Hawaaz, reads one line, then works the
reply inbox. Everything is computed from stored runs; nothing is generated.
"""

from __future__ import annotations

from .config import FOCUS_BRAND
from .store import Store

OPEN = {"open", None}


def _brand_row(report, brand):
    if report is None:
        return None, None
    for i, b in enumerate(report.brands):
        if b.brand == brand:
            return i + 1, b
    return None, None


def build_brief(store: Store, focus: str = FOCUS_BRAND) -> dict:
    run = store.latest_run()
    if run is None:
        return {"has_data": False}
    prev = store.previous_run(run.run_id)
    now_rep = store.report(run.run_id, "all")
    prev_rep = store.report(prev.run_id, "all") if prev else None
    rank, me = _brand_row(now_rep, focus)
    prank, pme = _brand_row(prev_rep, focus)

    # Biggest keyword move for the focus brand
    mover = None
    if prev:
        for k in store.keywords(run.run_id):
            if k == "all":
                continue
            _, a = _brand_row(store.report(run.run_id, k), focus)
            _, b = _brand_row(store.report(prev.run_id, k), focus)
            if a and b:
                d = a.sov - b.sov
                if mover is None or abs(d) > abs(mover["delta"]):
                    mover = {"keyword": k, "delta": round(d, 4), "sov": a.sov}

    flags = store.risks(run.run_id, brand=focus, limit=100_000)
    status = store.risk_statuses()
    prev_keys = (
        {(f["comment_id"], f["brand"]) for f in store.risks(prev.run_id, brand=focus, limit=100_000)}
        if prev
        else set()
    )
    open_flags = [f for f in flags if status.get((f["comment_id"], f["brand"]), {}).get("status") in OPEN]
    new_high = [
        f for f in flags if f["severity"] == "high" and (f["comment_id"], f["brand"]) not in prev_keys
    ]
    open_safety = [f for f in open_flags if "safety" in f["categories"]]

    insights = store.insights(run.run_id)
    action = next((i for i in insights if i["kind"] == "alert"), None) or next(
        (i for i in insights if i["kind"] == "recommendation"), None
    )

    delta = round(me.sov - pme.sov, 4) if me and pme else None
    parts = []
    if me:
        parts.append(
            f"{focus} holds {100 * me.sov:.1f}% of the conversation, #{rank} of {len(now_rep.brands)}"
        )
        if delta is not None:
            word = "up" if delta > 0.0005 else "down" if delta < -0.0005 else "flat"
            parts[-1] += (
                f", {word} {abs(100 * delta):.1f} pts since the last run"
                if word != "flat"
                else ", flat since the last run"
            )
    if open_safety:
        parts.append(f"{len(open_safety)} safety comments are waiting for a reply")
    elif open_flags:
        parts.append(f"{len(open_flags)} risk comments are waiting for a reply")
    sentence = ". ".join(parts) + "." if parts else "No mentions of the focus brand yet."

    return {
        "has_data": True,
        "run": run.model_dump(),
        "previous_run_id": prev.run_id if prev else None,
        "focus_brand": focus,
        "sentence": sentence,
        "sov": me.sov if me else 0.0,
        "sov_previous": pme.sov if pme else None,
        "sov_delta": delta,
        "rank": rank,
        "rank_previous": prank,
        "brands_ranked": len(now_rep.brands) if now_rep else 0,
        "positive_share": me.positive_share if me else 0.0,
        "net_sentiment": me.net_sentiment if me else 0.0,
        "biggest_move": mover,
        "open_risks": len(open_flags),
        "open_safety": len(open_safety),
        "new_high_risks": len(new_high),
        "action": {"title": action["title"], "text": action["text"]} if action else None,
    }
