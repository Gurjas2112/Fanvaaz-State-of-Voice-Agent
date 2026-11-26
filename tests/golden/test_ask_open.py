"""Open questions: does Ask pick the right kind of answer, and is each answer safe and grounded?

ask_open.jsonl: 35 questions in four routes (data, app, general, off_topic), hand-labelled.

* offline (always): without a model, every data and app question is routed by the rules
  and never refused, and no off-topic question is answered.
* with Gemini (pytest -m llm): route accuracy over all 35, decline precision, and for the
  data answers the judge's faithfulness and numeric grounding. Writes reports/ask_open.json.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from fanvaaz.ask import Answerer

ROWS = [
    json.loads(x) for x in (Path(__file__).parent / "ask_open.jsonl").read_text("utf-8").splitlines() if x
]


@pytest.mark.golden
def test_routes_without_a_model(latest):
    a = Answerer(latest["videos"], latest["mentions"], latest["reports"])
    for r in ROWS:
        out = a.answer(r["q"])
        if r["route"] in ("data", "app"):
            assert out["route"] == r["route"] and not out["refused"], (r, out["reasoning_steps"])
        if r["route"] == "off_topic":
            assert out["refused"], r


@pytest.mark.llm
@pytest.mark.golden
def test_open_questions_with_gemini(latest, reports_dir):
    base, key = os.getenv("LLM_EVAL_BASE_URL"), os.getenv("LLM_EVAL_API_KEY")
    if not (base and key):
        pytest.skip("set LLM_EVAL_BASE_URL / LLM_EVAL_API_KEY for the open-question eval")
    from fanvaaz.config import Settings
    from fanvaaz.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(Settings(llm_base_url=base, llm_api_key=key, llm_enabled=True))
    a = Answerer(latest["videos"], latest["mentions"], latest["reports"], llm)
    results = []
    for r in ROWS:
        out = a.answer(r["q"])
        results.append(
            {
                "q": r["q"],
                "expected": r["route"],
                "got": out["route"],
                "mode": out["mode"],
                "refused": out["refused"],
                "faithfulness": out.get("faithfulness"),
                "rejected": out.get("rejected_llm_answer", False),
                "answer": out["answer"][:300],
            }
        )
    n = len(results)
    correct = sum(x["expected"] == x["got"] for x in results)
    declined = [x for x in results if x["refused"]]
    faith = [x["faithfulness"] for x in results if x["faithfulness"] is not None]
    report = {
        "questions": n,
        "route_accuracy": round(correct / n, 3),
        "confusion": dict(
            Counter(f"{x['expected']}->{x['got']}" for x in results if x["expected"] != x["got"])
        ),
        "decline_precision": round(sum(x["expected"] == "off_topic" for x in declined) / len(declined), 3)
        if declined
        else None,
        "off_topic_declined": sum(x["refused"] for x in results if x["expected"] == "off_topic"),
        "mean_faithfulness": round(sum(faith) / len(faith), 3) if faith else None,
        "llm_answers_rejected": sum(x["rejected"] for x in results),
        "results": results,
    }
    (reports_dir / "ask_open.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    assert report["route_accuracy"] >= 0.85, report["confusion"]
    assert report["off_topic_declined"] == sum(r["route"] == "off_topic" for r in ROWS)
    assert all(x["mode"] == "general" for x in results if x["got"] == "general")
