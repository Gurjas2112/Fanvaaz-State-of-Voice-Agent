"""Accuracy gates on the hand-labelled golden sets.

dev.jsonl     70 comments, used while writing the rules (expect it to look good).
holdout.jsonl 50 comments, written before the rules were tuned and evaluated once.
              The holdout number is the honest one; the README reports it.
Gates are set just under the measured holdout numbers so a regression fails CI.
"""

import json
from pathlib import Path

import pytest

from fanvaaz.evaluate import evaluate, load

GOLDEN = Path(__file__).parent

GATES = {
    "dev": {"brand_f1": 0.98, "sentiment_macro_f1": 0.90},
    "holdout": {"brand_f1": 0.95, "sentiment_macro_f1": 0.80},
}


@pytest.mark.golden
@pytest.mark.parametrize("split", ["dev", "holdout"])
def test_golden_accuracy(split, reports_dir):
    result = evaluate(load(GOLDEN / f"{split}.jsonl"))
    (reports_dir / f"golden_{split}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    gate = GATES[split]
    assert result["brand"]["f1"] >= gate["brand_f1"], result["errors"]
    assert result["sentiment"]["macro_f1"] >= gate["sentiment_macro_f1"], result["errors"]


@pytest.mark.golden
def test_golden_files_are_well_formed():
    for split in GATES:
        for row in load(GOLDEN / f"{split}.jsonl"):
            assert set(row) == {"text", "labels"}
            assert all(v in {"positive", "neutral", "negative"} for v in row["labels"].values())
