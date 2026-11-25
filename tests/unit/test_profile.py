"""Tracking profiles: Fanvaaz must work for any brand, not just the OnePlus example."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fanvaaz import profile
from fanvaaz.brands import all_brands, brands_in
from fanvaaz.config import Settings
from fanvaaz.extract import extract_video
from fanvaaz.guardrails import check_question
from fanvaaz.models import Comment, Video
from fanvaaz.risk import flag_comments
from fanvaaz.sentiment import score_text
from fanvaaz.sov import by_keyword

ATHER = "ev-scooters-india"


def base(**over) -> dict:
    p = {
        "id": "acme-kettles",
        "name": "Acme kettles",
        "category": "electric kettles",
        "focus_brand": "Acme",
        "brands": {"Acme": ["acme"], "Boilex": ["boilex"]},
        "keywords": ["best electric kettle"],
    }
    p.update(over)
    return p


def test_every_template_validates_and_the_test_profile_is_active():
    ps = profile.presets()
    assert {"smartphones-india", ATHER} <= set(ps)
    assert profile.active().id == "smartphones-india"  # FANVAAZ_PROFILE in conftest
    assert profile.active().focus_brand == "OnePlus"


def test_nothing_is_tracked_until_someone_chooses(monkeypatch):
    """A fresh install has no default brand: onboarding must pick one."""
    from fanvaaz.config import get_settings

    monkeypatch.setattr(get_settings(), "profile", "")
    profile.reset()
    assert profile.configured() is False
    with pytest.raises(profile.NoProfileError):
        profile.active()


@pytest.mark.parametrize(
    "over, message",
    [
        ({"focus_brand": "Nobody"}, "must be one of the tracked brands"),
        ({"brands": {"Acme": ["acme"]}}, "between 2 and"),
        ({"brands": {"Acme": ["acme", "ab"], "Boilex": ["ab"]}}, "listed for both"),
        ({"brands": {"Acme": ["a"], "Boilex": []}}, "too short"),
        ({"keywords": []}, "at least one"),
        ({"keywords": [f"k{i}" for i in range(11)]}, "at most 10"),
        ({"playbook": {"taste": "x"}}, "matching aspect"),
        ({"risk_terms": {"vibes": ["meh"]}}, "risk categories"),
        ({"id": "Has Spaces"}, "lowercase"),
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_bad_profiles_are_rejected_with_a_reason(over, message):
    with pytest.raises(ValidationError, match=message):
        profile.Profile.model_validate(base(**over))


def test_brand_name_is_always_an_alias_and_aliases_are_deduplicated():
    p = profile.Profile.model_validate(base(brands={"Acme": ["ACME", "acme pro"], "Boilex": []}))
    assert p.brands == {"Acme": ["acme", "acme pro"], "Boilex": ["boilex"]}


def test_switching_profile_switches_brand_detection():
    text = "Ather 450X beats Ola S1 on range, OnePlus phones are another story"
    assert brands_in(text) == ["OnePlus"]
    profile.set_active(profile.load(ATHER))
    assert brands_in(text) == ["Ather", "Ola Electric"]
    assert "Samsung" not in all_brands()


def test_sentiment_and_aspects_follow_the_profile():
    profile.set_active(profile.load(ATHER))
    res = score_text("Ather is better than Ola S1, the range per charge is great")
    assert res["Ather"].sentiment == "positive"
    assert res["Ola Electric"].sentiment == "negative"
    assert "range" in res["Ather"].aspects


def test_profile_risk_terms_add_to_the_generic_ones():
    def video(text):
        return Video(
            video_id="v1",
            keyword="k",
            rank=1,
            title="t",
            comments=[Comment(comment_id="c1", text=text, like_count=3)],
        )

    def flags(text):
        v = video(text)
        return flag_comments([v], extract_video(v))

    assert flags("Ola S1 brake failure on the highway") == []  # not a phone-world term
    profile.set_active(profile.load(ATHER))
    got = flags("Ola S1 brake failure on the highway")
    assert got and got[0]["brand"] == "Ola Electric" and "safety" in got[0]["categories"]
    # Generic terms still apply in every category.
    assert flags("Chetak service: no response for 2 weeks, going to consumer court")


def test_ask_topic_guard_follows_the_profile():
    q = "How far does the range go on scooters?"
    assert "off_topic" in check_question(q).reasons
    profile.set_active(profile.load(ATHER))
    assert check_question(q).allowed
    assert "off_topic" in check_question("Best pizza in Pune?").reasons


def test_share_of_voice_runs_end_to_end_on_a_custom_profile():
    profile.set_active(profile.Profile.model_validate(base()))
    videos = [
        Video(
            video_id=f"v{r}",
            keyword="best electric kettle",
            rank=r,
            title="Acme vs Boilex kettle review" if r == 1 else "Boilex kettle review",
            view_count=1000,
            like_count=50,
            comment_count=2,
            comments=[Comment(comment_id=f"c{r}", text="Acme boils faster, love it")],
        )
        for r in (1, 2)
    ]
    mentions = [m for v in videos for m in extract_video(v)]
    reports = by_keyword(videos, mentions, Settings())
    names = [b.brand for b in reports["all"].brands]
    assert set(names) == {"Acme", "Boilex"}
    assert abs(sum(b.sov for b in reports["all"].brands) - 1) < 1e-6
