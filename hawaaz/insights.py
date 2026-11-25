"""Insights and recommendations for Atomberg's content and marketing team.

Every insight is computed deterministically and carries its evidence (the exact
numbers it states). If an LLM is configured it may rewrite the wording, but the
rewrite is accepted only if every number in it is found in that evidence; else
the deterministic text is kept. The harness tests this with a lying fake LLM.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict

from .config import FOCUS_BRAND
from .guardrails import safe_output, ungrounded_numbers
from .llm import LLM, parse_json
from .models import KeywordReport, Mention, Video
from .sov import rank_weight

log = logging.getLogger("hawaaz.insights")

REWRITE_SYSTEM = """You polish marketing insights for Atomberg's content team.
Rewrite each insight's text to be clear and actionable in at most 2 sentences.
Use ONLY the numbers given in its evidence. Do not add any new number, brand, or claim.
Reply with JSON only: {"items": [{"id": "...", "text": "..."}]}"""

ASPECT_PLAYBOOK = {
    "remote & app": "publish short how-to videos on remote pairing and app setup, and pin them under top reviews",
    "noise": "lead creatives with silent-operation demos recorded on camera",
    "energy saving": "show a before/after electricity bill comparison in creator content",
    "service": "route these complaints to service ops and showcase installation turnaround",
    "price": "frame price as payback: savings per year against the price premium",
    "air delivery": "publish air-delivery tests at each speed next to competitor fans",
    "build & design": "feature finishes and design in Shorts aimed at home-decor audiences",
}


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _brand(report: KeywordReport, brand: str):
    for i, b in enumerate(report.brands):
        if b.brand == brand:
            return i + 1, b
    return None, None


def build(
    reports: dict[str, KeywordReport],
    videos: list[Video],
    mentions: list[Mention],
    focus: str = FOCUS_BRAND,
    risks: list[dict] | None = None,
) -> list[dict]:
    out: list[dict] = []
    overall = reports.get("all")
    if not overall or not overall.brands:
        return out

    rank, me = _brand(overall, focus)
    leader = overall.brands[0]
    if me:
        rival = leader if leader.brand != focus else (overall.brands[1] if len(overall.brands) > 1 else None)
        text = f"{focus} ranks #{rank} on overall Share of Voice at {pct(me.sov)}"
        ev: dict = {"atomberg_sov": me.sov, "atomberg_rank": rank}
        if rival:
            text += f"; {rival.brand} is {'next' if rank == 1 else 'ahead'} at {pct(rival.sov)}."
            ev |= {"rival_sov": rival.sov}
        out.append(
            {
                "id": "headline",
                "kind": "headline",
                "title": "Overall Share of Voice",
                "text": text,
                "evidence": ev,
            }
        )

        # Visibility vs advocacy vs engagement
        gap = me.positive_share - me.mention_share
        if gap > 0.02:
            t = (
                f"Viewers speak well of {focus} more often than they mention it: positive share {pct(me.positive_share)} "
                f"vs mention share {pct(me.mention_share)}. Amplify existing happy-customer comments and reviews."
            )
        elif gap < -0.02:
            t = (
                f"{focus} is mentioned often but praised less: mention share {pct(me.mention_share)} vs positive share "
                f"{pct(me.positive_share)}. Fix the complaint themes below before buying more reach."
            )
        else:
            t = (
                f"{focus}'s mention share ({pct(me.mention_share)}) and positive share ({pct(me.positive_share)}) "
                f"are balanced."
            )
        out.append(
            {
                "id": "advocacy",
                "kind": "insight",
                "title": "Visibility vs advocacy",
                "text": t,
                "evidence": {"positive_share": me.positive_share, "mention_share": me.mention_share},
            }
        )

        if me.engagement_share + 0.02 < me.mention_share:
            out.append(
                {
                    "id": "engagement",
                    "kind": "recommendation",
                    "title": "Reach the high-engagement videos",
                    "text": (
                        f"{focus}'s engagement share ({pct(me.engagement_share)}) trails its mention share "
                        f"({pct(me.mention_share)}): it is discussed in smaller videos. Prioritise creator "
                        f"partnerships on the top-ranked, high-view reviews."
                    ),
                    "evidence": {"engagement_share": me.engagement_share, "mention_share": me.mention_share},
                }
            )

    # Keyword strengths and gaps
    kw_rows = []
    for k, r in reports.items():
        if k == "all":
            continue
        rk, b = _brand(r, focus)
        top = r.brands[0] if r.brands else None
        kw_rows.append((k, rk, b.sov if b else 0.0, top))
    if kw_rows:
        best = max(kw_rows, key=lambda x: x[2])
        worst = min(kw_rows, key=lambda x: x[2])
        out.append(
            {
                "id": "best_keyword",
                "kind": "insight",
                "title": "Strongest keyword",
                "text": f'{focus} is strongest on "{best[0]}" with {pct(best[2])} Share of Voice.',
                "evidence": {"sov": best[2]},
            }
        )
        if worst[0] != best[0] and worst[3] is not None:
            rep = reports[worst[0]]
            leader_kw = worst[3]
            if leader_kw.brand != focus:
                text = (
                    f'On "{worst[0]}" {focus} has only {pct(worst[2])} Share of Voice while '
                    f"{leader_kw.brand} leads with {pct(leader_kw.sov)}. Target this query with "
                    f"comparison videos and SEO-optimised titles."
                )
                ev = {"sov": worst[2], "leader_sov": leader_kw.sov}
            else:
                runner = rep.brands[1] if len(rep.brands) > 1 else None
                text = (
                    f'"{worst[0]}" is {focus}\'s weakest keyword: it still leads with {pct(worst[2])}'
                    + (f", but {runner.brand} is close at {pct(runner.sov)}" if runner else "")
                    + ". Defend it with comparison videos and SEO-optimised titles."
                )
                ev = {"sov": worst[2], "runner_sov": runner.sov if runner else 0.0}
            out.append(
                {
                    "id": "weak_keyword",
                    "kind": "recommendation",
                    "title": "Keyword to defend or win back",
                    "text": text,
                    "evidence": ev,
                }
            )

    # Why: aspects behind positive and negative mentions of the focus brand
    pos_aspects, neg_aspects = Counter(), Counter()
    n_pos = n_neg = 0
    for m in mentions:
        if m.brand != focus:
            continue
        if m.sentiment == "positive":
            n_pos += 1
            pos_aspects.update(m.aspects)
        elif m.sentiment == "negative":
            n_neg += 1
            neg_aspects.update(m.aspects)
    if pos_aspects:
        a, c = pos_aspects.most_common(1)[0]
        out.append(
            {
                "id": "praise",
                "kind": "recommendation",
                "title": "Lead with what people praise",
                "text": (
                    f'"{a}" drives the most praise: {c} of {n_pos} positive {focus} mentions. '
                    f"Make it the first claim in ads and creator briefs."
                ),
                "evidence": {"count": c, "total": n_pos},
            }
        )
    if neg_aspects:
        a, c = neg_aspects.most_common(1)[0]
        action = ASPECT_PLAYBOOK.get(a, "address it in content and with the product team")
        out.append(
            {
                "id": "complaints",
                "kind": "recommendation",
                "title": "Top complaint theme",
                "text": f'"{a}" is the top complaint: {c} of {n_neg} negative {focus} mentions. Action: {action}.',
                "evidence": {"count": c, "total": n_neg},
            }
        )

    # Channels: who already talks about the brand, and which top videos don't
    by_channel: dict[str, float] = defaultdict(float)
    slot_brands: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in mentions:
        slot_brands[(m.keyword, m.video_id)].add(m.brand)
    for v in videos:
        if focus in slot_brands[(v.keyword, v.video_id)]:
            by_channel[v.channel_title] += rank_weight(v.rank)
    gaps = sorted(
        [
            v
            for v in videos
            if v.rank <= 10
            and focus not in slot_brands[(v.keyword, v.video_id)]
            and slot_brands[(v.keyword, v.video_id)]
        ],
        key=lambda v: v.view_count,
        reverse=True,
    )
    if by_channel:
        tops = [c for c, _ in sorted(by_channel.items(), key=lambda x: x[1], reverse=True)[:3] if c]
        if tops:
            out.append(
                {
                    "id": "advocates",
                    "kind": "recommendation",
                    "title": "Channels already talking about you",
                    "text": f"Top channels mentioning {focus} in high-ranked results: {', '.join(tops)}. "
                    f"Offer them early access to launches.",
                    "evidence": {},
                }
            )
    if gaps:
        chans = []
        for v in gaps:
            if v.channel_title and v.channel_title not in chans:
                chans.append(v.channel_title)
            if len(chans) == 3:
                break
        out.append(
            {
                "id": "gaps",
                "kind": "recommendation",
                "title": "Top-10 videos that skip you",
                "text": (
                    f"{len(gaps)} top-10 videos mention competitors but not {focus}. Highest-view channels to "
                    f"approach: {', '.join(chans)}."
                ),
                "evidence": {"count": len(gaps)},
            }
        )
    # Risk radar (adapted from the CRIF hackathon offense rating; see risk.py)
    if risks:
        mine = [r for r in risks if r["brand"] == focus]
        safety = [r for r in mine if "safety" in r["categories"]]
        high = [r for r in mine if r["severity"] == "high"]
        if mine:
            cats = Counter(c for r in mine for c in r["categories"])
            top_cat, top_n = cats.most_common(1)[0]
            text = (
                f"{len(mine)} {focus} comments carry escalation risk ({len(high)} high severity). "
                f"Most common: {top_cat.replace('_', ' ')} ({top_n})."
            )
            if safety:
                text += (
                    f" {len(safety)} mention a safety issue such as sparking or burning: route them to "
                    f"the quality team today and reply publicly within 24 hours."
                )
            out.append(
                {
                    "id": "risk_own",
                    "kind": "alert",
                    "title": "Risk radar: escalate",
                    "text": text,
                    "evidence": {"total": len(mine), "high": len(high), "top": top_n, "safety": len(safety)},
                }
            )
        rivals = Counter(
            r["brand"] for r in risks if r["brand"] != focus and "product_failure" in r["categories"]
        )
        if rivals:
            rb, rn = rivals.most_common(1)[0]
            out.append(
                {
                    "id": "risk_rival",
                    "kind": "recommendation",
                    "title": "Competitor weak spot",
                    "text": (
                        f"{rb} has the most product-failure complaints among competitors ({rn}). "
                        f"Reliability and warranty content can win those buyers."
                    ),
                    "evidence": {"count": rn},
                }
            )
    for ins in out:
        ins["text"] = safe_output(ins["text"])
        ins["source"] = "rules"
    return out


def evidence_values(ins: dict) -> list[float]:
    vals = []
    for v in ins.get("evidence", {}).values():
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def rewrite_with_llm(insights: list[dict], llm: LLM) -> list[dict]:
    """Let an LLM polish wording; keep a rewrite only if it is numerically grounded."""
    payload = [{"id": i["id"], "text": i["text"], "evidence": i.get("evidence", {})} for i in insights]
    try:
        reply = parse_json(llm.complete(REWRITE_SYSTEM, json.dumps({"items": payload}), json_mode=True))
        new = {it["id"]: it["text"] for it in reply.get("items", []) if isinstance(it, dict) and "text" in it}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM rewrite failed, keeping rule text: %s", e)
        return insights
    out = []
    for ins in insights:
        cand = new.get(ins["id"])
        if not cand:
            out.append(ins)
            continue
        bad = ungrounded_numbers(cand, evidence_values(ins))
        if bad:
            log.warning("rejected ungrounded rewrite for %s: %s", ins["id"], bad)
            out.append({**ins, "rejected_rewrite": True})
        else:
            out.append({**ins, "text": safe_output(cand), "source": "llm"})
    return out
