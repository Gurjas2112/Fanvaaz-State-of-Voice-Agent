"""The Ask agent as a LangGraph state graph.

    guard -> recall -> plan -> stats -> cache --hit--> respond
                                          \\-miss-> retrieve -> context -> answer -> verify -> remember -> respond
    guard --refused--> respond

* guard     injection / PII / off-topic checks, with an injection risk score
* recall    short-term memory of this thread + long-term preferences
* plan      the reasoning step: resolve follow-ups from memory, split comparisons
            into one sub-query per brand; an LLM planner only when the rules
            find nothing, and it may only name known brands and aspects
* stats     computed numbers for the scope (the only numbers an answer may use)
* cache     verified semantic answer cache (same run, same scope, similar
            question, cited comments still present)
* retrieve  hybrid retrieval per sub-query (BM25 + vectors, RRF, re-rank)
* context   token-budgeted context with citation ids
* answer    extractive always; LLM through the gateway when allowed
* verify    numeric grounding, citation check, injection check on the output
* remember  write the turn to memory and the answer to the cache

The result includes `reasoning_steps`: a structured trace of each decision,
never hidden chain-of-thought.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import ai, cache, obs, prompts
from .. import context as context_mod
from .. import profile as profile_mod
from ..brands import brands_in, find_spans
from ..guardrails import REFUSAL, check_question, is_injection, numbers_in, safe_output, ungrounded_numbers
from ..memory import Memory
from ..schemas import AnswerOut, PlanOut

FOLLOW_UP = ("and ", "what about", "how about", "same for", "aur ", "and?", "vs ", "compared to")
PRAISE = {
    "praise",
    "praises",
    "like",
    "likes",
    "love",
    "loves",
    "good",
    "best",
    "positive",
    "happy",
    "appreciate",
    "strength",
    "strengths",
    "pasand",
    "achha",
    "accha",
}
COMPLAIN = {
    "complain",
    "complains",
    "complaint",
    "complaints",
    "problem",
    "problems",
    "issue",
    "issues",
    "bad",
    "hate",
    "negative",
    "worst",
    "weakness",
    "weaknesses",
    "shikayat",
    "dislike",
}


def intent_of(question: str) -> str | None:
    words = set(re.findall(r"[a-z]+", question.lower()))
    if words & COMPLAIN:
        return "negative"
    if words & PRAISE:
        return "positive"
    return None


def without_brands(question: str) -> str:
    """The keyword query minus brand names: the brand filter already applies them."""
    out, last = [], 0
    for sp in find_spans(question):
        out.append(question[last : sp.start])
        last = sp.end
    out.append(question[last:])
    return " ".join("".join(out).split())


class AskState(TypedDict, total=False):
    question: str
    user_key: str | None
    thread_id: str | None
    verdict: dict
    memory: dict
    prefs: dict
    plan: list[dict]
    scope: dict
    stats: list[str]
    ev: dict
    sources: list[dict]
    context: dict
    data: str
    citations: dict
    cached: dict | None
    llm_answer: dict | None
    steps: list[str]
    result: dict


def build(answerer):
    a = answerer
    s_mem = Memory(a.store) if a.store is not None else None

    def node(name):
        def wrap(fn):
            def run_node(state: AskState):
                with obs.span("ask", name):
                    return fn(state) or {}

            return run_node

        return wrap

    @node("guard")
    def guard(state):
        v = check_question(state["question"])
        steps = [
            f"guard: injection risk {v.risk_score}; "
            + ("allowed" if v.allowed else f"refused ({', '.join(v.reasons)})")
        ]
        return {
            "verdict": {"allowed": v.allowed, "reasons": v.reasons, "risk_score": v.risk_score},
            "steps": steps,
        }

    @node("recall")
    def recall(state):
        mem, prefs = {"turns": [], "summary": "", "last_scope": None}, {"brands": [], "aspects": []}
        if s_mem is not None and state.get("user_key"):
            if state.get("thread_id"):
                mem = s_mem.recall(state["thread_id"], state["user_key"])
            prefs = s_mem.prefs(state["user_key"])
        n = len([t for t in mem["turns"] if t["role"] == "user"])
        step = f"recall: {n} earlier question(s) in this thread" + (
            f"; saved focus {prefs['brands'] or prefs['aspects']}"
            if prefs["brands"] or prefs["aspects"]
            else ""
        )
        return {"memory": mem, "prefs": prefs, "steps": state["steps"] + [step]}

    @node("plan")
    def plan(state):
        q = state["question"]
        low = q.strip().lower()
        prof = profile_mod.active()
        named = brands_in(q)
        aspect = a._aspect(q)
        keyword = a._keyword(q)
        steps = list(state["steps"])
        last = (state.get("memory") or {}).get("last_scope") or {}
        is_follow_up = bool(last) and (low.startswith(FOLLOW_UP) or len(low.split()) <= 5)
        if not named:
            if is_follow_up and last.get("brands"):
                named = list(last["brands"])
                steps.append(f"plan: follow-up, kept brands {named} from the previous question")
            elif state["prefs"].get("brands"):
                named = [b for b in state["prefs"]["brands"] if b in prof.brands][:3]
                steps.append(f"plan: no brand named, used saved focus {named}")
        if aspect is None and is_follow_up and last.get("aspect"):
            aspect = last["aspect"]
            steps.append(f"plan: follow-up, kept aspect '{aspect}'")
        if keyword == "all" and is_follow_up and last.get("keyword") not in (None, "all"):
            keyword = last["keyword"]
        intent = intent_of(q) or (last.get("intent") if is_follow_up else None)
        if not named and aspect is None and a.llm is not None:
            res = ai.call(
                a.llm,
                prompts.get("plan", category=prof.category),
                f"<lists>{json.dumps({'brands': list(prof.brands), 'aspects': list(prof.aspects)})}</lists>\n"
                f"Question: {q}",
                task="plan",
                node="ask.plan",
                schema=PlanOut,
            )
            if res.ok and isinstance(res.parsed, PlanOut):
                named = [st.brand for st in res.parsed.steps if st.brand in prof.brands]
                aspect = next((st.aspect for st in res.parsed.steps if st.aspect in prof.aspects), None)
                steps.append(
                    f"plan: LLM planner proposed {named or 'no known brand'} / {aspect or 'no aspect'}"
                )
        if not named:
            named = [prof.focus_brand]
            steps.append(f"plan: no brand named, defaulted to the focus brand {named[0]}")
        named = named[:3]
        sub = [{"brand": b, "aspect": aspect, "keyword": keyword, "intent": intent} for b in named]
        kind = "comparison" if len(named) > 1 else "single brand"
        steps.append(
            f"plan: {kind}; {len(sub)} sub-quer{'y' if len(sub) == 1 else 'ies'} "
            f"({', '.join(named)}) on '{keyword}'" + (f", aspect '{aspect}'" if aspect else "")
        )
        if intent:
            steps.append(f"plan: looking for {'praise' if intent == 'positive' else 'complaints'}")
        scope = {"brands": named, "keyword": keyword, "aspect": aspect, "intent": intent}
        return {"plan": sub, "scope": scope, "steps": steps}

    @node("stats")
    def stats(state):
        sc = state["scope"]
        lines, ev = a._evidence(sc["brands"], sc["keyword"], sc["aspect"])
        return {"stats": lines, "ev": ev, "steps": state["steps"] + [f"stats: {len(lines)} computed fact(s)"]}

    @node("cache")
    def cache_lookup(state):
        if a.store is None or not a.run_id:
            return {"cached": None}
        hit = cache.find_answer(a.store, a.run_id, state["scope"], state["question"], None, a.valid_ids)
        if hit is None:
            return {"cached": None, "steps": state["steps"] + ["cache: miss"]}
        answer, sim = hit
        obs.record_call(
            obs.CallRecord(
                node="ask.answer",
                model="cache",
                prompt_id="ask",
                prompt_version="-",
                cache_hit=True,
                outcome="cached",
            )
        )
        return {
            "cached": {**answer, "cache": {"hit": True, "similarity": sim}},
            "steps": state["steps"] + [f"cache: verified hit (similarity {sim}, citations still in the run)"],
        }

    @node("retrieve")
    def retrieve(state):
        q = state["question"]
        per = max(6 // len(state["plan"]), 2)
        merged, seen = [], set()
        if a.retriever.has_vectors:
            legs = "hybrid (BM25 + vectors)"
        elif a.embedder is None:
            legs = "lexical (BM25): no embedding model available"
        else:
            legs = "lexical (BM25): this run has no stored vectors"
        for sub in state["plan"]:
            words = _aspect_words(sub["aspect"])
            query = without_brands(q) + " " + " ".join(words[:4])
            hits = a.retriever.search(
                query,
                k=per + 2,
                must_brand=sub["brand"],
                aspect_words=words,
                want_sentiment=sub.get("intent"),
                sentiment_of=a.sentiment_of,
            )
            if not hits:
                hits = a.retriever.search(query, k=per + 2, aspect_words=words)
            for h in hits[: per + 2]:
                if h.chunk.doc_id in seen:
                    continue
                seen.add(h.chunk.doc_id)
                merged.append(
                    {
                        "comment_id": h.chunk.doc_id,
                        "video_id": h.chunk.video_id,
                        "keyword": h.chunk.keyword,
                        "text": safe_output(h.chunk.text),
                        "score": round(h.score, 4),
                        "lexical_rank": h.lexical_rank,
                        "vector_rank": h.vector_rank,
                        "features": h.features,
                        "for_brand": sub["brand"],
                    }
                )
        merged = merged[:6]
        return {
            "sources": merged,
            "steps": state["steps"] + [f"retrieve: {len(merged)} comment(s) via {legs}, re-ranked"],
        }

    @node("context")
    def build_context(state):
        ctx = context_mod.build(
            state["stats"],
            state["sources"],
            (state.get("memory") or {}).get("summary", ""),
            budget_tokens=2500,
        )
        r = ctx.report
        return {
            "data": ctx.data,
            "citations": ctx.citations,
            "context": r,
            "steps": state["steps"]
            + [
                f"context: {r['used_tokens']} of {r['budget_tokens']} tokens, "
                f"{r['comments_kept']} cited comment(s)"
            ],
        }

    @node("answer")
    def answer(state):
        if a.llm is None:
            return {"llm_answer": None}
        prof = profile_mod.active()
        res = ai.call(
            a.llm,
            prompts.get("ask", focus=prof.focus_brand, category=prof.category),
            f"<data>{state['data']}</data>\n\nQuestion: {state['question']}",
            task="ask",
            node="ask.answer",
            schema=AnswerOut,
        )
        if isinstance(res.parsed, AnswerOut):
            out = {
                "text": res.parsed.answer,
                "citations": res.parsed.citations,
                "answerable": res.parsed.answerable,
                "schema_ok": True,
                "cached": res.cached,
            }
        elif res.text:  # a reply that is not JSON: still checked below, but uncited
            out = {"text": res.text, "citations": [], "answerable": True, "schema_ok": False, "cached": False}
        else:
            return {
                "llm_answer": None,
                "steps": state["steps"] + [f"answer: LLM unavailable ({res.outcome})"],
            }
        return {
            "llm_answer": out,
            "steps": state["steps"]
            + [
                f"answer: LLM replied ({'valid schema' if out['schema_ok'] else 'free text'}"
                f"{', response cache' if out['cached'] else ''})"
            ],
        }

    @node("verify")
    def verify(state):
        steps = list(state["steps"])
        sources = list(state["citations"].values()) if state.get("citations") else []
        extractive = " ".join(state["stats"])
        if sources:
            extractive += " What viewers say: " + " | ".join(f'"{s["text"][:160]}"' for s in sources[:3])
        result = {
            "answer": safe_output(extractive),
            "refused": False,
            "reasons": [],
            "sources": [{k: v for k, v in s.items()} for s in sources],
            "mode": "extractive",
            "scope": state["scope"],
            "citations": [s["citation"] for s in sources],
            "context": state.get("context", {}),
            "cache": {"hit": False},
        }
        llm = state.get("llm_answer")
        if llm is None:
            steps.append("verify: extractive answer from computed stats and quotes")
            return {"result": {**result, "reasoning_steps": steps}}
        reply = llm["text"]
        evidence = [float(v) for v in state["ev"].values() if isinstance(v, (int, float))]
        evidence += [n for s in sources for n, _ in numbers_in(s["text"])]  # quoted numbers are grounded too
        bad_numbers = ungrounded_numbers(reply, evidence)
        unknown_cites = [c for c in llm["citations"] if c not in state["citations"]]
        if bad_numbers or is_injection(reply):
            reason = f"ungrounded numbers {bad_numbers}" if bad_numbers else "answer looked like an injection"
            steps.append(f"verify: rejected the LLM answer ({reason}); kept the extractive answer")
            return {
                "result": {
                    **result,
                    "rejected_llm_answer": True,
                    "fallback_reason": reason,
                    "reasoning_steps": steps,
                }
            }
        cited = [c for c in llm["citations"] if c in state["citations"]]
        steps.append(
            f"verify: numbers grounded; {len(cited)} valid citation(s)"
            + (f", dropped {len(unknown_cites)} unknown" if unknown_cites else "")
        )
        return {
            "result": {
                **result,
                "answer": safe_output(reply.strip()),
                "mode": "llm",
                "citations": cited or result["citations"],
                "answerable": llm["answerable"],
                "reasoning_steps": steps,
            }
        }

    @node("remember")
    def remember(state):
        res = state["result"]
        if a.store is not None and a.run_id and res["mode"] in ("llm", "extractive") and not res["refused"]:
            cache.put_answer(
                a.store,
                a.run_id,
                state["scope"],
                state["question"],
                None,
                {k: res[k] for k in ("answer", "mode", "sources", "citations", "scope")},
            )
        if s_mem is not None and state.get("user_key") and state.get("thread_id"):
            s_mem.remember(
                state["thread_id"], state["user_key"], state["question"], res["answer"], state["scope"]
            )
            s_mem.learn(state["user_key"], state["scope"])
        return {}

    @node("respond")
    def respond(state):
        v = state["verdict"]
        if not v["allowed"]:
            result = {
                "answer": REFUSAL,
                "refused": True,
                "reasons": v["reasons"],
                "sources": [],
                "mode": "guard",
                "citations": [],
                "reasoning_steps": state["steps"],
                "cache": {"hit": False},
            }
        elif state.get("cached"):
            c = state["cached"]
            result = {
                "answer": c["answer"],
                "refused": False,
                "reasons": [],
                "sources": c["sources"],
                "mode": c["mode"],
                "scope": state["scope"],
                "citations": c.get("citations", []),
                "cache": c["cache"],
                "reasoning_steps": state["steps"],
            }
        else:
            result = state["result"]
        result["risk_score"] = v.get("risk_score", 0.0)
        result["thread_id"] = state.get("thread_id")
        result["trace_id"] = obs.current_trace()
        return {"result": result}

    g = StateGraph(AskState)
    for name, fn in [
        ("guard", guard),
        ("recall", recall),
        ("plan", plan),
        ("stats", stats),
        ("cache", cache_lookup),
        ("retrieve", retrieve),
        ("context", build_context),
        ("answer", answer),
        ("verify", verify),
        ("remember", remember),
        ("respond", respond),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges(
        "guard",
        lambda s: "recall" if s["verdict"]["allowed"] else "respond",
        {"recall": "recall", "respond": "respond"},
    )
    g.add_edge("recall", "plan")
    g.add_edge("plan", "stats")
    g.add_edge("stats", "cache")
    g.add_conditional_edges(
        "cache",
        lambda s: "respond" if s.get("cached") else "retrieve",
        {"respond": "respond", "retrieve": "retrieve"},
    )
    g.add_edge("retrieve", "context")
    g.add_edge("context", "answer")
    g.add_edge("answer", "verify")
    g.add_edge("verify", "remember")
    g.add_edge("remember", "respond")
    g.add_edge("respond", END)
    return g.compile()


def _aspect_words(aspect: str | None) -> list[str]:
    if not aspect:
        return []
    return [aspect, *profile_mod.active().aspects.get(aspect, [])]


def new_thread() -> str:
    return uuid.uuid4().hex[:16]


def mermaid() -> str:
    class _A:
        store = None
        run_id = None
        llm = None

    return build(_A()).get_graph().draw_mermaid()
