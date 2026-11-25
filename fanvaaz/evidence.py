"""Evidence generation: every claim points at the data behind it.

Each insight already carries `evidence` (the exact numbers it states). This module
adds `citations`: the videos and comments a reviewer would open to check it.

  praise / complaints     the focus brand's most-liked positive / negative
                          comments on that aspect
  share-of-voice claims   the highest-ranked videos where the brand is mentioned
  risk alerts             the highest-scoring flagged comments
  channel advice          the videos from those channels

`verify()` re-checks every insight before it is stored: its numbers must be in its
evidence (the grounding rule) and each citation must exist in the run. The pack
(`/api/v1/runs/{run_id}/evidence`) is the audit artefact for one run.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from .guardrails import ungrounded_numbers
from .models import Mention, RunInfo, Video

MAX_CITES = 5


def _video_cite(v: Video) -> dict:
    return {
        "type": "video",
        "video_id": v.video_id,
        "keyword": v.keyword,
        "rank": v.rank,
        "title": v.title[:140],
        "channel": v.channel_title,
        "url": f"https://www.youtube.com/watch?v={v.video_id}",
    }


def _comment_cite(cid: str, v: Video, text: str, likes: int) -> dict:
    return {
        "type": "comment",
        "comment_id": cid,
        "video_id": v.video_id,
        "keyword": v.keyword,
        "text": text[:240],
        "like_count": likes,
        "url": f"https://www.youtube.com/watch?v={v.video_id}&lc={cid}",
    }


def attach(
    insights: list[dict], videos: list[Video], mentions: list[Mention], risks: list[dict], focus: str
) -> list[dict]:
    comments = {c.comment_id: (c, v) for v in videos for c in v.comments}
    slots: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in mentions:
        slots[(m.keyword, m.video_id)].add(m.brand)
    focus_videos = sorted(
        [v for v in videos if focus in slots[(v.keyword, v.video_id)]], key=lambda v: (v.rank, -v.view_count)
    )
    for ins in insights:
        cites: list[dict] = []
        kind = ins["id"]
        if kind in ("praise", "complaints"):
            want = "positive" if kind == "praise" else "negative"
            aspect = ins["text"].split('"')[1] if ins["text"].count('"') >= 2 else None
            pool = [
                m
                for m in mentions
                if m.brand == focus
                and m.source == "comment"
                and m.sentiment == want
                and (aspect is None or aspect in m.aspects)
                and m.source_id in comments
            ]
            pool.sort(key=lambda m: comments[m.source_id][0].like_count, reverse=True)
            for m in pool[:MAX_CITES]:
                c, v = comments[m.source_id]
                cites.append(_comment_cite(c.comment_id, v, c.text, c.like_count))
        elif kind in ("risk_own", "risk_rival"):
            rel = [r for r in risks if (r["brand"] == focus) == (kind == "risk_own")]
            if kind == "risk_rival":
                rel = [r for r in rel if "product_failure" in r["categories"]]
            for r in rel[:MAX_CITES]:
                cites.append(
                    {
                        "type": "comment",
                        "comment_id": r["comment_id"],
                        "video_id": r["video_id"],
                        "keyword": r["keyword"],
                        "text": r["evidence"][:240],
                        "like_count": r["like_count"],
                        "url": f"https://www.youtube.com/watch?v={r['video_id']}&lc={r['comment_id']}",
                    }
                )
        elif kind == "gaps":
            gaps = [
                v
                for v in videos
                if v.rank <= 10
                and focus not in slots[(v.keyword, v.video_id)]
                and slots[(v.keyword, v.video_id)]
            ]
            cites = [
                _video_cite(v) for v in sorted(gaps, key=lambda v: v.view_count, reverse=True)[:MAX_CITES]
            ]
        elif kind in ("best_keyword", "weak_keyword"):
            kw = ins["text"].split('"')[1] if ins["text"].count('"') >= 2 else None
            cites = [_video_cite(v) for v in focus_videos if v.keyword == kw][:MAX_CITES]
            if not cites and kw:
                cites = [
                    _video_cite(v)
                    for v in sorted((v for v in videos if v.keyword == kw), key=lambda v: v.rank)
                ][:3]
        else:  # headline, advocacy, engagement, advocates
            cites = [_video_cite(v) for v in focus_videos[:MAX_CITES]]
        ins["citations"] = cites
    return insights


def verify(insights: list[dict], videos: list[Video]) -> list[dict]:
    """Grounding and citation checks; failures are flagged on the insight, never silently dropped."""
    video_ids = {v.video_id for v in videos}
    comment_ids = {c.comment_id for v in videos for c in v.comments}
    for ins in insights:
        vals = [float(v) for v in ins.get("evidence", {}).values() if isinstance(v, (int, float))]
        bad_numbers = ungrounded_numbers(ins["text"], vals)
        bad_cites = [
            c
            for c in ins.get("citations", [])
            if (c["type"] == "video" and c["video_id"] not in video_ids)
            or (c["type"] == "comment" and c["comment_id"] not in comment_ids)
        ]
        ins["verified"] = not bad_numbers and not bad_cites
        if bad_numbers:
            ins["ungrounded_numbers"] = bad_numbers
        if bad_cites:
            ins["missing_citations"] = len(bad_cites)
    return insights


def pack(info: RunInfo, insights: list[dict], risks: list[dict], settings) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run": info.model_dump(),
        "method": {
            "share_of_voice": (
                f"SoV = {settings.w_mentions}*M + {settings.w_engagement}*E + {settings.w_positive}*P, "
                "rank-weighted with 1/log2(rank+1) over the top-N search results per keyword"
            ),
            "brands": "deterministic alias matcher; an LLM can never add or remove a mention",
            "sentiment": "rule engine; optional LLM re-check of low-confidence labels, schema-validated",
            "grounding": "every number in an insight must appear in its evidence",
        },
        "insights": [
            {
                k: ins.get(k)
                for k in (
                    "id",
                    "kind",
                    "title",
                    "text",
                    "source",
                    "evidence",
                    "citations",
                    "verified",
                    "ungrounded_numbers",
                )
            }
            for ins in insights
        ],
        "verified_share": round(sum(1 for i in insights if i.get("verified")) / len(insights), 3)
        if insights
        else None,
        "top_risks": risks[:20],
    }
