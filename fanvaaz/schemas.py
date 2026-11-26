"""Output contracts for every LLM task.

A reply is parsed into one of these models; anything that does not validate is a
schema failure, counted in the AI Ops panel (schema-valid rate) and retried once
with the validation error attached. The JSON Schema of each model is also sent as
`response_format` so providers that support structured output constrain the reply.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Label = Literal["positive", "neutral", "negative"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BrandLabel(_Out):
    brand: str = Field(min_length=1, max_length=60)
    sentiment: Label


class SentimentItem(_Out):
    id: str = Field(min_length=1, max_length=200)
    labels: list[BrandLabel] = Field(default_factory=list, max_length=20)


class SentimentBatchOut(_Out):
    items: list[SentimentItem] = Field(max_length=200)


class RewriteItem(_Out):
    id: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=600)


class RewriteOut(_Out):
    items: list[RewriteItem] = Field(max_length=50)


class AnswerOut(_Out):
    answer: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(default_factory=list, max_length=12)
    answerable: bool = True


class PlanStep(_Out):
    brand: str = Field(min_length=1, max_length=60)
    aspect: str | None = Field(default=None, max_length=60)
    intent: Literal["compare", "explain", "count"] = "explain"


class PlanOut(_Out):
    steps: list[PlanStep] = Field(min_length=1, max_length=3)


Route = Literal["data", "app", "general", "off_topic"]


class RouteOut(_Out):
    route: Route
    reason: str = Field(default="", max_length=200)


class ToolCall(_Out):
    tool: str = Field(min_length=1, max_length=40)
    args: dict = Field(default_factory=dict)


class AgentOut(_Out):
    calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    done: bool = False


class ClaimCheck(_Out):
    n: int = Field(ge=1, le=40)
    supported: bool


class JudgeOut(_Out):
    claims: list[ClaimCheck] = Field(default_factory=list, max_length=40)


class GeneralOut(_Out):
    answer: str = Field(min_length=1, max_length=1500)


REGISTRY: dict[str, type[BaseModel]] = {
    "SentimentBatchOut": SentimentBatchOut,
    "RewriteOut": RewriteOut,
    "AnswerOut": AnswerOut,
    "PlanOut": PlanOut,
    "RouteOut": RouteOut,
    "AgentOut": AgentOut,
    "JudgeOut": JudgeOut,
    "GeneralOut": GeneralOut,
}


def json_schema(model: type[BaseModel]) -> dict:
    """A provider-friendly JSON Schema: inlined, no titles, no defaults."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [inline(x) for x in node]
        return node

    return inline(schema)
