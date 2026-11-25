"""Typed records that flow through the pipeline and out of the API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Sentiment = Literal["positive", "neutral", "negative"]


class Comment(BaseModel):
    comment_id: str
    text: str  # PII already redacted before it is stored
    like_count: int = 0
    author_hash: str = ""  # sha256 of the channel id, never the name


class Video(BaseModel):
    video_id: str
    keyword: str
    rank: int = Field(ge=1)
    title: str
    description: str = ""
    channel_title: str = ""
    published_at: str = ""
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    comments: list[Comment] = Field(default_factory=list)


class Mention(BaseModel):
    """One brand mentioned in one piece of text (a title, description or comment)."""

    video_id: str
    keyword: str
    source: Literal["title", "description", "comment"]
    source_id: str
    brand: str
    sentiment: Sentiment
    aspects: list[str] = Field(default_factory=list)
    # |rule score|: how sure the rule engine was. Not stored; decides which mentions the LLM re-checks.
    confidence: float = Field(default=0.0, exclude=True)


class BrandMetrics(BaseModel):
    brand: str
    mentions: float  # rank-weighted mention count
    mention_share: float  # M
    engagement_share: float  # E
    positive_share: float  # P (Share of Positive Voice)
    sov: float  # weighted composite
    net_sentiment: float  # (pos - neg) / all mentions of this brand, -1..1
    raw_mentions: int
    positive: int
    negative: int
    videos_mentioning: int


class KeywordReport(BaseModel):
    keyword: str
    videos: int
    comments: int
    brands: list[BrandMetrics]


class RunInfo(BaseModel):
    run_id: str
    # "youtube": collected live. "replay": re-scored from a recorded real collection (tests, audits).
    source: Literal["youtube", "replay"]
    created_at: str
    keywords: list[str]
    videos: int
    comments: int
    quota_units_used: int = 0
    # The tracking profile this run used.
    profile_id: str = ""
    focus_brand: str = ""
    brands: list[str] = Field(default_factory=list)
