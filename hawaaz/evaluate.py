"""Accuracy of brand detection and brand-targeted sentiment against a labelled set.

Brand detection: micro precision / recall / F1 over (comment, brand) pairs.
Sentiment: accuracy and macro-F1 over pairs where the brand was correctly found
(a missed brand is already charged to detection recall).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .extract import label_text

LABELS = ("positive", "neutral", "negative")


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def evaluate(rows: list[dict], predict=label_text) -> dict:
    tp = fp = fn = 0
    conf = Counter()
    errors = []
    for row in rows:
        gold: dict = row["labels"]
        pred = predict(row["text"])
        g, p = set(gold), set(pred)
        tp += len(g & p)
        fp += len(p - g)
        fn += len(g - p)
        for b in g & p:
            conf[(gold[b], pred[b])] += 1
            if gold[b] != pred[b]:
                errors.append({"text": row["text"], "brand": b, "gold": gold[b], "pred": pred[b]})
        for b in g ^ p:
            errors.append({"text": row["text"], "brand": b, "gold": gold.get(b), "pred": pred.get(b)})
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    per_label = {}
    for lab in LABELS:
        ltp = conf[(lab, lab)]
        lfp = sum(v for (g, p), v in conf.items() if p == lab and g != lab)
        lfn = sum(v for (g, p), v in conf.items() if g == lab and p != lab)
        lp = ltp / (ltp + lfp) if ltp + lfp else 0.0
        lr = ltp / (ltp + lfn) if ltp + lfn else 0.0
        per_label[lab] = {
            "precision": round(lp, 4),
            "recall": round(lr, 4),
            "f1": round(_f1(lp, lr), 4),
            "support": ltp + lfn,
        }
    matched = sum(conf.values())
    return {
        "examples": len(rows),
        "brand_pairs": tp + fn,
        "brand": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(_f1(precision, recall), 4),
        },
        "sentiment": {
            "accuracy": round(sum(conf[(lab, lab)] for lab in LABELS) / matched, 4) if matched else 0.0,
            "macro_f1": round(sum(v["f1"] for v in per_label.values()) / len(LABELS), 4),
            "per_label": per_label,
        },
        "errors": errors,
    }
