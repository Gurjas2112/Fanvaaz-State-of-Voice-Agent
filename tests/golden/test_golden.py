"""Accuracy gates on the hand-labelled golden sets.

Hand-written sets (short, clean sentences):
  dev.jsonl           70 comments, used while writing the rules (expect it to look good).
  holdout.jsonl       50 comments, written before the rules were tuned and evaluated once.
Real YouTube comments (from the recorded snapshot, labelled by hand; Hinglish, emoji, typos):
  real_dev.jsonl      53 comments: the rules were tuned on its errors.
  real_holdout.jsonl  54 comments: never used for tuning. This is the honest number.
The real sets were sampled with the brand matcher, so they measure sentiment only;
brand recall is measured on the hand-written sets.
Gates sit just under the measured numbers so a regression fails CI.
"""

import json
from pathlib import Path

import pytest

from fanvaaz.evaluate import evaluate, load

GOLDEN = Path(__file__).parent

GATES = {
    "dev": {"brand_f1": 0.98, "sentiment_macro_f1": 0.90},
    "holdout": {"brand_f1": 0.95, "sentiment_macro_f1": 0.80},
    "real_dev": {"brand_f1": 0.99, "sentiment_macro_f1": 0.80},
    "real_holdout": {"brand_f1": 0.99, "sentiment_macro_f1": 0.70},
}


@pytest.mark.golden
@pytest.mark.parametrize("split", list(GATES))
def test_golden_accuracy(split, reports_dir):
    result = evaluate(load(GOLDEN / f"{split}.jsonl"))
    (reports_dir / f"golden_{split}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    gate = GATES[split]
    assert result["brand"]["f1"] >= gate["brand_f1"], result["errors"]
    assert result["sentiment"]["macro_f1"] >= gate["sentiment_macro_f1"], result["errors"]


@pytest.mark.golden
def test_golden_files_are_well_formed():
    for split in GATES:
        for row in load(GOLDEN / f"{split}.jsonl"):
            assert set(row) - {"id"} == {"text", "labels"}
            assert all(v in {"positive", "neutral", "negative"} for v in row["labels"].values())
