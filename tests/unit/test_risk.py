import pytest

from atompulse.models import Comment, Mention, Video
from atompulse.risk import find_risks, flag_comments, reach_multiplier, severity, summarize


def flags_for(*texts, likes=0, mentions=None):
    v = Video(
        video_id="v",
        keyword="smart fan",
        rank=1,
        title="t",
        comments=[Comment(comment_id=f"c{i}", text=t, like_count=likes) for i, t in enumerate(texts)],
    )
    return flag_comments([v], mentions or [])


@pytest.mark.parametrize(
    "text, category",
    [
        ("Atomberg fan started sparking near the regulator", "safety"),
        ("Burning smell from the motor", "safety"),
        ("Will go to consumer court", "legal_consumer"),
        ("Motor burnt within 6 months", "product_failure"),
        ("Technician never came", "service_failure"),
        ("fan jal gaya", "safety"),
    ],
)
def test_categories(text, category):
    assert category in {h.category for h in find_risks(text)}


@pytest.mark.parametrize(
    "text",
    [
        "No sparking issue at all",  # negated
        "Never had a burning smell",  # negated
        "fir se dekha, accha hai",  # Hinglish 'fir' = 'phir', not a police FIR
        "Great fan, silent and saves power",
    ],
)
def test_no_false_risk(text):
    cats = {h.category for h in find_risks(text)}
    assert "safety" not in cats and "legal_consumer" not in cats


@pytest.mark.xfail(strict=True, reason="known limit: slang 'this fan is fire' (= great) reads as a fire risk")
def test_slang_fire_is_not_a_safety_risk():
    assert "safety" not in {h.category for h in find_risks("This fan is fire, love it")}


def test_risk_goes_to_nearest_brand_not_every_brand():
    out = flags_for(
        "Havells caught fire, switched to Atomberg",
        "Switched to Atomberg after my old Crompton started sparking",
    )
    assert {(f["brand"], tuple(f["categories"])) for f in out} == {
        ("Havells", ("safety",)),
        ("Crompton", ("safety",)),
    }


def test_brandless_risk_clause_attaches_only_in_one_brand_comments():
    assert [f["brand"] for f in flags_for("Bought Atomberg. It stopped working in a week")] == ["Atomberg"]
    assert flags_for("Atomberg or Havells? Mine stopped working") == []


def test_comment_without_brand_is_not_flagged():
    assert flags_for("my fan started sparking") == []


def test_safety_is_always_high_and_likes_raise_score():
    low = flags_for("Atomberg sparking", likes=0)[0]
    viral = flags_for("Atomberg sparking", likes=999)[0]
    assert low["severity"] == viral["severity"] == "high"
    assert viral["score"] > low["score"]
    assert reach_multiplier(0) == 1.0 and reach_multiplier(99) == pytest.approx(2.0)


def test_negative_mention_adds_weight():
    m = [
        Mention(
            video_id="v",
            keyword="smart fan",
            source="comment",
            source_id="c0",
            brand="Atomberg",
            sentiment="negative",
        )
    ]
    assert flags_for("Atomberg remote not working", mentions=m)[0]["score"] == 30
    assert flags_for("Atomberg remote not working")[0]["score"] == 20


def test_severity_thresholds():
    assert severity(10, ["service_failure"]) == "low"
    assert severity(35, ["product_failure"]) == "medium"
    assert severity(65, ["product_failure", "legal_consumer"]) == "high"
    assert severity(5, ["safety"]) == "high"


def test_each_category_counts_once_per_comment():
    f = flags_for("Atomberg stopped working, then broke again, totally defective")[0]
    assert f["categories"] == ["product_failure"] and f["score"] == 20


def test_summary_counts():
    s = summarize(flags_for("Atomberg sparking", "Atomberg stopped working", "Havells refund pending"))
    assert s["Atomberg"]["total"] == 2 and s["Atomberg"]["high"] == 1
    assert s["Havells"]["by_category"]["legal_consumer"] == 1


def test_evidence_keeps_every_triggering_clause():
    f = flags_for("Atomberg service: no response for 2 weeks. Will go to consumer court")[0]
    assert f["categories"] == ["legal_consumer", "service_failure"]
    assert "no response" in f["evidence"] and "consumer court" in f["evidence"]
