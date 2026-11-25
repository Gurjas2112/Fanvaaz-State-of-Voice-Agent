"""Metamorphic properties: changes to the input that must NOT change (or must
predictably change) the output. They catch bugs no hand-written example would."""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from fanvaaz.config import Settings
from fanvaaz.extract import extract_all, label_text
from fanvaaz.models import Comment, Video
from fanvaaz.sov import compute

S = Settings()
BRANDS = ["OnePlus", "Samsung", "Xiaomi", "Realme", "Vivo"]
PHRASES = [
    "{b} is great",
    "{b} is noisy",
    "{b} vs {c}?",
    "{b} is better than {c}",
    "nice video",
    "{b} remote broke",
]

comment_text = st.builds(
    lambda p, b, c: p.format(b=b, c=c),
    st.sampled_from(PHRASES),
    st.sampled_from(BRANDS),
    st.sampled_from(BRANDS),
)


@st.composite
def dataset(draw):
    n = draw(st.integers(1, 8))
    videos = []
    for i in range(n):
        texts = draw(st.lists(comment_text, min_size=1, max_size=6))
        videos.append(
            Video(
                video_id=f"v{i}",
                keyword="smart phone",
                rank=i + 1,
                title=draw(comment_text),
                view_count=draw(st.integers(0, 10**7)),
                like_count=draw(st.integers(0, 10**5)),
                comment_count=len(texts),
                comments=[Comment(comment_id=f"v{i}c{j}", text=t) for j, t in enumerate(texts)],
            )
        )
    return videos


def sov_map(videos):
    rep = compute(videos, extract_all(videos), S)
    return {b.brand: round(b.sov, 6) for b in rep.brands}


@settings(max_examples=60, deadline=None)
@given(dataset(), st.randoms())
def test_comment_order_does_not_matter(videos, rnd: random.Random):
    before = sov_map(videos)
    for vid in videos:
        rnd.shuffle(vid.comments)
    assert sov_map(videos) == before


@settings(max_examples=60, deadline=None)
@given(dataset())
def test_brandless_comments_do_not_change_sov(videos):
    before = sov_map(videos)
    for vid in videos:
        vid.comments.append(Comment(comment_id=vid.video_id + "-noise", text="Nice video bhai, subscribed"))
    assert sov_map(videos) == before


@settings(max_examples=60, deadline=None)
@given(dataset())
def test_alias_spelling_does_not_change_sov(videos):
    before = sov_map(videos)
    for vid in videos:
        for c in vid.comments:
            c.text = c.text.replace("OnePlus", "one plus")
        vid.title = vid.title.replace("OnePlus", "ONEPLUS")
    assert sov_map(videos) == before


@settings(max_examples=40, deadline=None)
@given(dataset())
def test_shares_are_valid_fractions(videos):
    rep = compute(videos, extract_all(videos), S)
    for b in rep.brands:
        for x in (b.mention_share, b.engagement_share, b.positive_share, b.sov):
            assert 0.0 <= x <= 1.0
        assert -1.0 <= b.net_sentiment <= 1.0


@given(st.sampled_from(BRANDS), st.sampled_from(BRANDS))
def test_comparative_is_antisymmetric(a, b):
    if a == b:
        return
    got = label_text(f"{a} is better than {b}")
    flipped = label_text(f"{b} is worse than {a}")
    assert got[a] == flipped[a] == "positive"
    assert got[b] == flipped[b] == "negative"
