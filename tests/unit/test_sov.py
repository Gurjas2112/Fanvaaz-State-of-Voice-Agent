import math

import pytest

from atompulse.config import Settings
from atompulse.models import Comment, Mention, Video
from atompulse.sov import compute, engagement, rank_weight

S = Settings(w_mentions=0.3, w_engagement=0.4, w_positive=0.3)


def v(vid, rank, views=1000, likes=10, comments=5, kw="smart fan"):
    return Video(
        video_id=vid,
        keyword=kw,
        rank=rank,
        title="t",
        view_count=views,
        like_count=likes,
        comment_count=comments,
        comments=[Comment(comment_id=f"{vid}-c", text="x")],
    )


def m(vid, brand, source="comment", sentiment="neutral", sid=None, kw="smart fan"):
    return Mention(
        video_id=vid,
        keyword=kw,
        source=source,
        source_id=sid or f"{vid}-{brand}-{source}",
        brand=brand,
        sentiment=sentiment,
    )


def test_rank_weight_is_dcg_discount():
    assert rank_weight(1) == 1.0
    assert rank_weight(3) == pytest.approx(0.5)
    assert rank_weight(50) < rank_weight(10) < rank_weight(2)
    with pytest.raises(ValueError):
        rank_weight(0)


def test_hand_computed_example():
    # Video 1 (rank 1): Atomberg in title (2) + 1 positive comment; Havells 1 negative comment.
    # Video 2 (rank 3): Havells 1 positive comment.
    videos = [v("a", 1), v("b", 3)]
    mentions = [
        m("a", "Atomberg", "title"),
        m("a", "Atomberg", "comment", "positive"),
        m("a", "Havells", "comment", "negative"),
        m("b", "Havells", "comment", "positive"),
    ]
    rep = compute(videos, mentions, S)
    row = {r.brand: r for r in rep.brands}
    # M: Atomberg = 1*(2+1) = 3 ; Havells = 1*1 + 0.5*1 = 1.5 -> shares 2/3, 1/3
    assert row["Atomberg"].mention_share == pytest.approx(2 / 3, abs=1e-4)
    # E: video a splits e(a) 3:1, video b all Havells with weight 0.5; e(a) == e(b)
    e = engagement(videos[0])
    ea, eh = 0.75 * e, 0.25 * e + 0.5 * e
    assert row["Atomberg"].engagement_share == pytest.approx(ea / (ea + eh), abs=1e-4)
    # P: Atomberg 1*1, Havells 0.5*1
    assert row["Atomberg"].positive_share == pytest.approx(2 / 3, abs=1e-4)
    assert row["Havells"].net_sentiment == 0.0  # 1 pos, 1 neg
    sov = 0.3 * (2 / 3) + 0.4 * (ea / (ea + eh)) + 0.3 * (2 / 3)
    assert row["Atomberg"].sov == pytest.approx(sov, abs=1e-4)


def test_shares_sum_to_one(latest):
    for rep in latest["reports"].values():
        for field in ("mention_share", "engagement_share", "positive_share", "sov"):
            assert sum(getattr(b, field) for b in rep.brands) == pytest.approx(1.0, abs=2e-3)


def test_empty_inputs():
    rep = compute([v("a", 1)], [], S)
    assert rep.brands == []


def test_engagement_is_log_damped():
    small, viral = v("a", 1, views=10_000), v("b", 1, views=10_000_000)
    assert engagement(viral) / engagement(small) < 2  # 1000x views is not 1000x weight
    assert math.isfinite(engagement(v("c", 1, views=0, likes=0, comments=0)))


def test_mentions_for_unknown_videos_are_ignored():
    rep = compute([v("a", 1)], [m("zzz", "Atomberg"), m("a", "Havells")], S)
    assert [b.brand for b in rep.brands] == ["Havells"]


def test_same_comment_counted_once_per_brand():
    rep = compute([v("a", 1)], [m("a", "Atomberg", sid="c1"), m("a", "Atomberg", sid="c1")], S)
    assert rep.brands[0].raw_mentions == 1
