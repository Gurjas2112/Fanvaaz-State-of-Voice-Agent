"""The LLM gateway: the one door every model call goes through.

    call(llm, prompt, user, task=..., schema=...) ->
      estimate tokens -> governance (kill switch, task, model, context, budget)
      -> response cache -> model -> validate against the output schema
      (one repair retry) -> cost -> ledger + cache

Callers get a `Result` and decide what to do with a refusal or a failure; in
Fanvaaz that is always "keep the rule-based answer". Fakes used in tests only
need a `complete(system, user, json_mode=...)` method; real clients also expose
`complete_ex`, which reports token usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from . import cache, obs, schemas, tokens
from .config import get_settings
from .governance import Governance
from .llm import LLM, Reply, parse_json
from .prompts import Prompt

log = logging.getLogger("fanvaaz.ai")


@dataclass
class Result:
    ok: bool
    text: str = ""
    parsed: BaseModel | None = None
    outcome: str = "ok"
    cached: bool = False
    usage: dict = field(default_factory=dict)


def _send(llm: LLM, system: str, user: str, schema: type[BaseModel] | None) -> Reply:
    if hasattr(llm, "complete_ex"):
        return llm.complete_ex(
            system,
            user,
            json_mode=schema is not None,
            json_schema=schemas.json_schema(schema) if schema else None,
        )
    text = llm.complete(system, user, json_mode=schema is not None)
    return Reply(
        text=text,
        prompt_tokens=tokens.estimate_messages(system, user),
        completion_tokens=tokens.estimate(text),
    )


def _validate(text: str, schema: type[BaseModel]) -> tuple[BaseModel | None, str]:
    try:
        return schema.model_validate(parse_json(text)), ""
    except (ValueError, ValidationError) as e:
        return None, str(e)[:300]


def call(
    llm: LLM | None,
    prompt: Prompt,
    user: str,
    *,
    task: str,
    node: str | None = None,
    schema: type[BaseModel] | None = None,
    use_cache: bool = True,
) -> Result:
    if llm is None:
        return Result(False, outcome="no_llm")
    s = get_settings()
    store = obs.bound()
    node = node or task
    model = getattr(llm, "model", s.llm_model)
    est = tokens.estimate_messages(prompt.system, user)
    rec = obs.CallRecord(
        node=node, model=model, prompt_id=prompt.id, prompt_version=prompt.version, est_prompt_tokens=est
    )

    decision = Governance(s, store).authorize(task, model, est, run_id=obs.current_run())
    if decision.allowed and getattr(llm, "circuit_open", None):
        decision = type(decision)(False, "circuit_open")
    if not decision.allowed:
        rec.outcome = f"refused:{decision.reason}"
        obs.record_call(rec)
        return Result(False, outcome=rec.outcome)

    key = cache.response_key(model, prompt.sha, user)
    # Only clients that declare themselves deterministic and cacheable use the cache (test fakes do not).
    use_cache = use_cache and bool(getattr(llm, "cacheable", False))
    if use_cache:
        hit = cache.get_response(store, key)
        if hit is not None:
            parsed, _ = _validate(hit, schema) if schema else (None, "")
            rec.cache_hit, rec.outcome, rec.schema_ok = (
                True,
                "cached",
                (parsed is not None) if schema else None,
            )
            obs.record_call(rec)
            return Result(True, text=hit, parsed=parsed, outcome="cached", cached=True)

    def attempt(message: str) -> Reply | None:
        try:
            return _send(llm, prompt.system, message, schema)
        except Exception as e:  # noqa: BLE001 - provider down, timeout, bad key: degrade quietly
            log.warning("LLM %s failed: %s", node, e)
            return None

    reply = attempt(user)
    if reply is None:
        rec.outcome = "error"
        obs.record_call(rec)
        return Result(False, outcome="error")

    def account(r: Reply, outcome: str, schema_ok: bool | None) -> None:
        r_rec = obs.CallRecord(
            node=node,
            model=r.model or model,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            est_prompt_tokens=est,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cached_tokens=r.cached_tokens,
            latency_ms=r.latency_ms,
            schema_ok=schema_ok,
            outcome=outcome,
            cost_usd=tokens.cost_usd(
                r.prompt_tokens,
                r.completion_tokens,
                r.cached_tokens,
                s.llm_price_in_per_m,
                s.llm_price_out_per_m,
                s.llm_price_cached_per_m,
            ),
        )
        obs.record_call(r_rec)

    usage = {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "cached_tokens": reply.cached_tokens,
        "estimated_prompt_tokens": est,
    }
    if schema is None:
        account(reply, "ok", None)
        if use_cache:
            cache.put_response(store, key, model, prompt.version, reply.text)
        return Result(True, text=reply.text, usage=usage)

    parsed, err = _validate(reply.text, schema)
    if parsed is None:
        account(reply, "invalid", False)
        repair = attempt(
            user + f"\n\nYour previous reply did not match the required JSON format ({err}). "
            "Reply again with JSON only, in exactly the format described."
        )
        if repair is None:
            return Result(False, text=reply.text, outcome="invalid")
        parsed, err = _validate(repair.text, schema)
        reply = repair
        if parsed is None:
            account(reply, "invalid", False)
            return Result(False, text=reply.text, outcome="invalid")
    account(reply, "ok", True)
    if use_cache:
        cache.put_response(store, key, model, prompt.version, reply.text)
    return Result(True, text=reply.text, parsed=parsed, usage=usage)
