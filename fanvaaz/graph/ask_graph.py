"""The Ask agent as a LangGraph state graph.

    guard --refused--> respond
    guard -> route --app-----> help ----> respond
                   --general-> general -> respond
                   --off_topic-------->  respond
                   --data--> recall -> plan -> agent -> stats -> cache --hit--> respond
                                \\-miss-> retrieve -> context -> answer -> verify -> judge -> remember -> respond

* guard     injection / PII / length checks, with an injection risk score
* route     rules first (brand named, data words, "how is SoV calculated"); the LLM
            router only for questions the rules cannot place
* help      fixed, accurate explanations of Fanvaaz itself (no model)
* general   category knowledge from the model, labelled "not from your data"
* recall    short-term memory of this thread + long-term preferences
* plan      the reasoning step: resolve follow-ups from memory, split comparisons
            into one sub-query per brand; an LLM planner only when the rules
            find nothing, and it may only name known brands and aspects
* agent     for rankings, trends, videos, risks: the model picks read-only data
            tools (tools.py); their outputs join the computed facts
* stats     computed numbers for the scope (the only numbers an answer may use)
* cache     verified semantic answer cache (same run, same scope, similar
            question, cited comments still present); skipped on "regenerate"
* retrieve  hybrid retrieval per sub-query (BM25 + vectors, RRF, re-rank)
* context   token-budgeted context with citation ids
* answer    extractive always; LLM through the gateway when allowed
* verify    numeric grounding, citation check, injection check on the output
* judge     claim check: each sentence against the data; unsupported ones are
            removed, and a mostly unsupported answer falls back to extractive
* remember  write the turn to memory and the answer to the cache

The result includes `reasoning_steps`: a structured trace of each decision,
never hidden chain-of-thought, plus `route`, `follow_ups` and `faithfulness`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import ai, cache, obs, prompts, router
from .. import context as context_mod
from .. import profile as profile_mod
from ..brands import brands_in, find_spans
from ..guardrails import REFUSAL, check_question, is_injection, numbers_in, safe_output, ungrounded_numbers
from ..memory import Memory
from ..schemas import AgentOut, AnswerOut, GeneralOut, JudgeOut, PlanOut, RouteOut
from ..tools import MAX_CALLS, DataTools

# Questions with these words need more than one brand's stats: the tool agent runs.
TOOL_HINTS = {
    "rank", "ranking", "ranks", "top", "most", "least", "which", "trend", "trending", "change", "changed",
    "since", "video", "videos", "channel", "channels", "risk", "risks", "escalation", "leads", "leader",
    "leading", "every", "overall", "biggest", "weakest", "strongest", "all",
}  # fmt: skip
HARD_REFUSALS = {"empty", "too_long", "prompt_injection", "pii_in_question"}
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\n+")

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
    fresh: bool
    verdict: dict
    route: str
    tool_lines: list[str]
    tool_ev: dict
    tool_sources: list[dict]
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
        hard = [r for r in v.reasons if r in HARD_REFUSALS]
        steps = [
            f"guard: injection risk {v.risk_score}; "
            + (f"refused ({', '.join(hard)})" if hard else "allowed")
        ]
        return {
            # off_topic is not a hard refusal: the router decides (general knowledge may still be answerable)
            "verdict": {
                "allowed": not hard,
                "reasons": hard,
                "risk_score": v.risk_score,
                "on_topic": "off_topic" not in v.reasons,
            },
            "steps": steps,
        }

    @node("route")
    def route(state):
        q = state["question"]
        on_topic = state["verdict"]["on_topic"]
        chosen, why = router.by_rules(q, on_topic)
        steps = list(state["steps"])
        if chosen is None and a.llm is not None:
            prof = profile_mod.active()
            res = ai.call(
                a.llm,
                prompts.get("route", category=prof.category, brands=", ".join(prof.brands)),
                f"Question: {q}",
                task="route",
                node="ask.route",
                schema=RouteOut,
            )
            if res.ok and isinstance(res.parsed, RouteOut):
                chosen, why = res.parsed.route, f"LLM router: {res.parsed.reason or 'no reason given'}"
        if chosen is None:
            chosen = router.fallback(on_topic)
            why += "; no model to decide, " + ("answered from the data" if chosen == "data" else "declined")
        steps.append(f"route: {chosen} ({why})")
        return {"route": chosen, "steps": steps}

    @node("help")
    def help_node(state):
        text = router.help_answer(state["question"])
        _remember_plain(state, text)
        return {
            "result": _plain_result(state, text, "help", ["help: fixed explanation of Fanvaaz (no model)"]),
        }

    @node("general")
    def general(state):
        prof = profile_mod.active()
        res = ai.call(
            a.llm,
            prompts.get("general", category=prof.category),
            f"Question: {state['question']}",
            task="general",
            node="ask.general",
            schema=GeneralOut,
        )
        reply = res.parsed.answer if res.ok and isinstance(res.parsed, GeneralOut) else ""
        if not reply or is_injection(reply):
            text = router.scope_message()
            return {
                "result": _plain_result(state, text, "guard", ["general: no usable model reply; declined"])
            }
        text = safe_output(reply.strip())
        _remember_plain(state, text)
        return {
            "result": _plain_result(
                state,
                text,
                "general",
                ["general: category knowledge from the model, not from the collected data"],
            )
        }

    def _plain_result(state, text: str, mode: str, extra: list[str]) -> dict:
        return {
            "answer": text,
            "refused": mode == "guard",
            "reasons": ["off_topic"] if mode == "guard" else [],
            "sources": [],
            "mode": mode,
            "citations": [],
            "cache": {"hit": False},
            "reasoning_steps": state["steps"] + extra,
        }

    def _remember_plain(state, text: str) -> None:
        if s_mem is not None and state.get("user_key") and state.get("thread_id"):
            s_mem.remember(state["thread_id"], state["user_key"], state["question"], text, {})

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

    @node("agent")
    def agent(state):
        empty = {"tool_lines": [], "tool_ev": {}, "tool_sources": []}
        q = state["question"]
        words = set(re.findall(r"[a-z]+", q.lower()))
        if a.llm is None or not (words & TOOL_HINTS):
            return empty
        prof = profile_mod.active()
        prompt = prompts.get(
            "agent",
            category=prof.category,
            brands=", ".join(prof.brands),
            aspects=", ".join(prof.aspects),
            keywords=", ".join(k for k in a.reports if k != "all"),
        )
        tools = DataTools(a)
        lines, ev, sources, steps, calls = [], {}, [], list(state["steps"]), 0
        for _round in range(2):
            res = ai.call(
                a.llm,
                prompt,
                f"<facts>{json.dumps(lines, ensure_ascii=False)}</facts>\n"
                f"Plan so far: {json.dumps(state['scope'])}\nQuestion: {q}",
                task="agent",
                node="ask.agent",
                schema=AgentOut,
            )
            if not (res.ok and isinstance(res.parsed, AgentOut)):
                steps.append(f"agent: no usable tool plan ({res.outcome}); kept the fixed plan")
                break
            for c in res.parsed.calls:
                if calls >= MAX_CALLS:
                    break
                calls += 1
                out = tools.call(c.tool, c.args)
                lines += out.lines
                ev |= out.evidence
                sources += out.sources
                shown = ", ".join(f"{k}={v}" for k, v in list(c.args.items())[:3])
                steps.append(
                    f"agent: {c.tool}({shown}) -> {len(out.lines)} fact(s), {len(out.sources)} comment(s)"
                )
            if res.parsed.done or not res.parsed.calls or calls >= MAX_CALLS:
                break
        return {"tool_lines": lines, "tool_ev": ev, "tool_sources": sources, "steps": steps}

    @node("stats")
    def stats(state):
        sc = state["scope"]
        lines, ev = a._evidence(sc["brands"], sc["keyword"], sc["aspect"])
        extra = state.get("tool_lines") or []
        lines = lines + [ln for ln in extra if ln not in lines]
        ev = {**ev, **(state.get("tool_ev") or {})}
        return {"stats": lines, "ev": ev, "steps": state["steps"] + [f"stats: {len(lines)} computed fact(s)"]}

    @node("cache")
    def cache_lookup(state):
        if a.store is None or not a.run_id:
            return {"cached": None}
        if state.get("fresh"):
            return {"cached": None, "steps": state["steps"] + ["cache: skipped (regenerate)"]}
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
        for s in (state.get("tool_sources") or [])[:3]:  # comments the agent searched for come first
            if s["comment_id"] not in seen:
                seen.add(s["comment_id"])
                merged.append(s)
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
                "_extractive": result["answer"],
            }
        }

    @node("judge")
    def judge(state):
        res = dict(state["result"])
        extractive = res.pop("_extractive", None)
        if res["mode"] != "llm" or a.llm is None:
            return {"result": res}
        steps = list(res["reasoning_steps"])
        sentences = [s.strip() for s in _SENT.split(res["answer"]) if s and s.strip()]
        if not sentences or len(sentences) > 40:
            return {"result": res}
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
        out = ai.call(
            a.llm,
            prompts.get("judge"),
            f"<data>{state['data']}</data>\n<answer>\n{numbered}\n</answer>",
            task="judge",
            node="ask.judge",
            schema=JudgeOut,
        )
        if not (
            out.ok and isinstance(out.parsed, JudgeOut) and out.parsed.claims
        ):  # silence is not a verdict
            steps.append(f"judge: unavailable ({out.outcome}); kept the verified answer")
            return {"result": {**res, "faithfulness": None, "reasoning_steps": steps}}
        unsupported = {c.n for c in out.parsed.claims if not c.supported and 1 <= c.n <= len(sentences)}
        faith = round(1 - len(unsupported) / len(sentences), 3)
        if not unsupported:
            steps.append(f"judge: all {len(sentences)} sentence(s) supported by the data")
            return {"result": {**res, "faithfulness": faith, "reasoning_steps": steps}}
        if len(unsupported) * 2 >= len(sentences) and extractive:
            steps.append(
                f"judge: {len(unsupported)} of {len(sentences)} sentence(s) unsupported; used the extractive answer"
            )
            return {
                "result": {
                    **res,
                    "answer": extractive,
                    "mode": "extractive",
                    "rejected_llm_answer": True,
                    "fallback_reason": "claims not supported by the data",
                    "faithfulness": faith,
                    "reasoning_steps": steps,
                }
            }
        kept = [s for i, s in enumerate(sentences, 1) if i not in unsupported]
        joiner = "\n" if "\n" in res["answer"] else " "
        steps.append(f"judge: removed {len(unsupported)} unsupported sentence(s) of {len(sentences)}")
        return {
            "result": {
                **res,
                "answer": joiner.join(kept),
                "removed_claims": [sentences[i - 1] for i in sorted(unsupported)],
                "faithfulness": faith,
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
        route_ = state.get("route")
        if v["allowed"] and route_ == "off_topic":
            result = _plain_result(state, router.scope_message(), "guard", [])
        elif v["allowed"] and route_ in ("app", "general"):
            result = state["result"]
        elif not v["allowed"]:
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
        result = dict(result)
        result.pop("_extractive", None)
        result["route"] = route_ or "guard"
        result["follow_ups"] = [] if result["refused"] else follow_ups(state.get("scope"), route_)
        result.setdefault("faithfulness", None)
        result["risk_score"] = v.get("risk_score", 0.0)
        result["thread_id"] = state.get("thread_id")
        result["trace_id"] = obs.current_trace()
        return {"result": result}

    g = StateGraph(AskState)
    for name, fn in [
        ("guard", guard),
        ("route", route),
        ("help", help_node),
        ("general", general),
        ("recall", recall),
        ("plan", plan),
        ("agent", agent),
        ("stats", stats),
        ("cache", cache_lookup),
        ("retrieve", retrieve),
        ("context", build_context),
        ("answer", answer),
        ("verify", verify),
        ("judge", judge),
        ("remember", remember),
        ("respond", respond),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges(
        "guard",
        lambda s: "route" if s["verdict"]["allowed"] else "respond",
        {"route": "route", "respond": "respond"},
    )
    g.add_conditional_edges(
        "route",
        lambda s: {"data": "recall", "app": "help", "general": "general"}.get(s["route"], "respond"),
        {"recall": "recall", "help": "help", "general": "general", "respond": "respond"},
    )
    g.add_edge("help", "respond")
    g.add_edge("general", "respond")
    g.add_edge("recall", "plan")
    g.add_edge("plan", "agent")
    g.add_edge("agent", "stats")
    g.add_edge("stats", "cache")
    g.add_conditional_edges(
        "cache",
        lambda s: "respond" if s.get("cached") else "retrieve",
        {"respond": "respond", "retrieve": "retrieve"},
    )
    g.add_edge("retrieve", "context")
    g.add_edge("context", "answer")
    g.add_edge("answer", "verify")
    g.add_edge("verify", "judge")
    g.add_edge("judge", "remember")
    g.add_edge("remember", "respond")
    g.add_edge("respond", END)
    return g.compile()


def follow_ups(scope: dict | None, route: str | None) -> list[str]:
    """Three next questions that use what the user just asked (and exercise memory: "And battery?")."""
    p = profile_mod.active()
    aspects = list(p.aspects)
    if route != "data" or not scope:
        a0 = aspects[0] if aspects else "price"
        return [
            f"What do viewers say about {p.focus_brand}'s {a0}?",
            f"Which brand leads the conversation on {p.category}?",
        ]
    brands = scope.get("brands") or [p.focus_brand]
    b = brands[0]
    aspect = scope.get("aspect")
    rival = next((r for r in [p.focus_brand, *p.rivals] if r not in brands), None)
    other = next((x for x in aspects if x != aspect), None)
    out = []
    if aspect and other:
        out.append(f"And {other}?")
    if scope.get("intent") == "negative":
        out.append(f"What do people praise about {b}" + (f"'s {aspect}?" if aspect else "?"))
    else:
        out.append(f"What do people complain about with {b}" + (f"'s {aspect}?" if aspect else "?"))
    if rival and len(brands) == 1:
        out.append(f"{b} vs {rival} on {aspect or (aspects[0] if aspects else 'price')}")
    elif not aspect and other:
        out.append(f"Which videos drive the most mentions of {b}?")
    return out[:3]


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
