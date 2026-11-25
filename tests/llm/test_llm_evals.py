"""LLM-graded evaluation with DeepEval and Ragas (needs a real LLM key).

Run:  FANVAAZ_LLM_BASE_URL=https://api.groq.com/openai/v1 \
      FANVAAZ_LLM_API_KEY=... pip install -e ".[llm-evals]" && pytest -m llm

The same OpenAI-compatible endpoint is used as the system under test and as the
judge. For a stricter setup, point the judge at a different, stronger model with
FANVAAZ_JUDGE_MODEL.
"""

from __future__ import annotations

import json
import os

import pytest

from fanvaaz.ask import Answerer
from fanvaaz.config import Settings
from fanvaaz.insights import build, rewrite_with_llm
from fanvaaz.llm import OpenAICompatLLM, parse_json

pytestmark = pytest.mark.llm

BASE = os.getenv("FANVAAZ_LLM_BASE_URL") or os.getenv("LLM_EVAL_BASE_URL")
KEY = os.getenv("FANVAAZ_LLM_API_KEY") or os.getenv("LLM_EVAL_API_KEY")
MODEL = os.getenv("LLM_EVAL_MODEL") or os.getenv("FANVAAZ_LLM_MODEL") or "gemini-flash-latest"
JUDGE = os.getenv("FANVAAZ_JUDGE_MODEL") or MODEL

if not (BASE and KEY):
    pytest.skip("LLM evals need FANVAAZ_LLM_BASE_URL and FANVAAZ_LLM_API_KEY", allow_module_level=True)

QUESTIONS = [
    "Why do people complain about OnePlus?",
    "What do viewers like about OnePlus phones?",
    "How does OnePlus compare with Samsung on battery?",
    "What do people say about the Samsung camera?",
    "Which brand leads on best camera phone India?",
]


def _llm(model: str) -> OpenAICompatLLM:
    return OpenAICompatLLM(Settings(llm_base_url=BASE, llm_api_key=KEY, llm_model=model))


@pytest.fixture(scope="module")
def answered(latest):
    a = Answerer(latest["videos"], latest["mentions"], latest["reports"], _llm(MODEL))
    rows = []
    for q in QUESTIONS:
        out = a.answer(q)
        brands, keyword, aspect = a._scope(q)
        stats, _ = a._evidence(brands, keyword, aspect)
        rows.append(
            {
                "question": q,
                "answer": out["answer"],
                "mode": out["mode"],
                "context": stats + [s["text"] for s in out["sources"]],
            }
        )
    return rows


def test_deepeval_faithfulness_and_relevancy(answered, reports_dir):
    deepeval = pytest.importorskip("deepeval")
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, HallucinationMetric
    from deepeval.models import DeepEvalBaseLLM
    from deepeval.test_case import LLMTestCase

    judge_llm = _llm(JUDGE)

    class Judge(DeepEvalBaseLLM):
        def load_model(self):
            return judge_llm

        def generate(self, prompt: str, schema=None):
            text = judge_llm.complete(
                "You are a careful evaluator. Reply in JSON when asked.", prompt, json_mode=schema is not None
            )
            return schema.model_validate(parse_json(text)) if schema is not None else text

        async def a_generate(self, prompt: str, schema=None):
            return self.generate(prompt, schema)

        def get_model_name(self):
            return f"judge:{JUDGE}"

    judge = Judge()
    results = []
    for row in answered:
        case = LLMTestCase(
            input=row["question"],
            actual_output=row["answer"],
            retrieval_context=row["context"],
            context=row["context"],
        )
        scores = {}
        for metric in (
            FaithfulnessMetric(threshold=0.8, model=judge, async_mode=False),
            AnswerRelevancyMetric(threshold=0.7, model=judge, async_mode=False),
            HallucinationMetric(threshold=0.3, model=judge, async_mode=False),
        ):
            metric.measure(case)
            scores[type(metric).__name__] = {
                "score": metric.score,
                "passed": metric.is_successful(),
                "reason": metric.reason,
            }
        results.append({"question": row["question"], "mode": row["mode"], "scores": scores})
    (reports_dir / "deepeval.json").write_text(
        json.dumps({"deepeval": deepeval.__version__, "judge": JUDGE, "results": results}, indent=2),
        encoding="utf-8",
    )
    faith = [r["scores"]["FaithfulnessMetric"]["score"] for r in results]
    assert sum(faith) / len(faith) >= 0.8, results


def test_deepeval_geval_recommendations_are_actionable(latest, reports_dir):
    pytest.importorskip("deepeval")
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    ins = rewrite_with_llm(build(latest["reports"], latest["videos"], latest["mentions"]), _llm(MODEL))
    recs = [i for i in ins if i["kind"] == "recommendation"]
    metric = GEval(
        name="Actionable recommendation",
        criteria=(
            "Is the recommendation specific (names a keyword, theme, channel or action) and actionable by "
            "a marketing team within a month, and consistent with the evidence numbers?"
        ),
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        threshold=0.6,
        async_mode=False,
    )
    scores = []
    for r in recs:
        metric.measure(LLMTestCase(input=json.dumps(r["evidence"]), actual_output=r["text"]))
        scores.append(metric.score)
    (reports_dir / "geval.json").write_text(
        json.dumps({"scores": scores, "recommendations": recs}, indent=2), encoding="utf-8"
    )
    assert sum(scores) / len(scores) >= 0.6


def test_ragas_faithfulness_and_context_precision(answered, reports_dir):
    ragas = pytest.importorskip("ragas")
    langchain_openai = pytest.importorskip("langchain_openai")
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithoutReference

    llm = LangchainLLMWrapper(
        langchain_openai.ChatOpenAI(base_url=BASE, api_key=KEY, model=JUDGE, temperature=0)
    )
    ds = EvaluationDataset.from_list(
        [
            {"user_input": r["question"], "response": r["answer"], "retrieved_contexts": r["context"]}
            for r in answered
        ]
    )
    result = evaluate(ds, metrics=[Faithfulness(llm=llm), LLMContextPrecisionWithoutReference(llm=llm)])
    df = result.to_pandas()
    summary = {c: float(df[c].mean()) for c in df.columns if df[c].dtype.kind == "f"}
    (reports_dir / "ragas.json").write_text(
        json.dumps({"ragas": ragas.__version__, "summary": summary}, indent=2), encoding="utf-8"
    )
    assert summary.get("faithfulness", 0) >= 0.8
