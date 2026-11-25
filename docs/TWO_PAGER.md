# AtomPulse: Atomberg's Share of Voice on YouTube

## Page 1: How it works

**AI agent:** searches YouTube for each keyword, reads the top N = 50 videos and up to 100 top comments each, finds brand mentions, scores brand-targeted sentiment, computes Share of Voice and writes recommendations. A guarded Q&A lets the team ask follow-up questions.

**Keywords (6):** smart fan, BLDC fan, best ceiling fan India, energy saving fan, ceiling fan with remote, smart ceiling fan app

**Why N = 50:** one YouTube search page returns 50 results; few viewers scroll past it, and the rank weight below already makes result 50 count 18% of result 1. Cost: ~151 API units per keyword, ~906 of the 10,000 free daily units for all six.

**Share of Voice metric** (per keyword, result at rank r weighted w = 1/log2(r+1)):

- M = share of rank-weighted mentions (title counts 2, description 1, each comment 1)
- E = share of rank-weighted engagement, ln(1+views) + 2 ln(1+likes) + 3 ln(1+comments), split among brands in the video
- P = Share of Positive Voice: share of rank-weighted positive mentions
- **SoV = 0.3 M + 0.4 E + 0.3 P** (weights configurable; all three reported separately)

| Layer | Tools |
| --- | --- |
| Collection | YouTube Data API v3 (httpx, retries, quota accounting, raw responses saved for replay) |
| NLP | Rule-based brand matcher (aliases, Hindi script); brand-targeted sentiment for English + Hinglish with comparatives, negation and contrast; optional LLM relabelling (any OpenAI-compatible model) |
| Safety | PII redaction at ingestion; prompt-injection quarantine for comments; question guardrails; numeric grounding check on every LLM output |
| Serving | FastAPI (OpenAPI), SQLite, responsive dashboard, Docker |
| Quality | pytest + Hypothesis, golden sets, DeepEval, Ragas, promptfoo, Postman/Newman, Schemathesis, Playwright, Appium, k6, GitHub Actions |

**Links:** code https://github.com/Gurjas2112/AtomPulse · live demo <add your deployed URL>

**Measured quality**

| Check | Result |
| --- | --- |
| Brand detection (held-out golden set, 59 pairs) | F1 1.00 |
| Brand-targeted sentiment, rules only (held-out) | macro-F1 0.84 |
| Red-team questions blocked | 40/40 |
| Benign questions wrongly refused | 0/30 |
| promptfoo behaviour + refusal checks | 11/11 pass |
| API p95 latency under k6 load (55 req/s) | 6.9 ms |

## Page 2: Findings and recommendations

> **TEMPLATE ONLY: the numbers below come from the synthetic demo dataset, not YouTube.** Run `atompulse run --source youtube` and re-run this script before submitting.

Run `20261125T075013-8c9114` · 300 result slots · 5661 comments · source: synthetic-demo

| Keyword | Atomberg SoV | Rank | Leader (SoV) | Atomberg positive share |
| --- | --- | --- | --- | --- |
| BLDC fan | 33.9% | 1 of 10 | Atomberg (33.9%) | 42.3% |
| All keywords | 29.5% | 1 of 10 | Atomberg (29.5%) | 34.3% |
| best ceiling fan India | 24.2% | 1 of 10 | Atomberg (24.2%) | 24.5% |
| ceiling fan with remote | 22.9% | 1 of 10 | Atomberg (22.9%) | 28.4% |
| energy saving fan | 35.6% | 1 of 10 | Atomberg (35.6%) | 40.5% |
| smart ceiling fan app | 29.4% | 1 of 10 | Atomberg (29.4%) | 33.4% |
| smart fan | 31.2% | 1 of 10 | Atomberg (31.2%) | 36.7% |

**Insights and recommendations**

1. **Overall Share of Voice.** Atomberg ranks #1 on overall Share of Voice at 29.5%; Havells is next at 17.9%.
2. **Visibility vs advocacy.** Viewers speak well of Atomberg more often than they mention it: positive share 34.3% vs mention share 27.6%. Amplify existing happy-customer comments and reviews.
3. **Strongest keyword.** Atomberg is strongest on "energy saving fan" with 35.6% Share of Voice.
4. **Keyword to defend or win back.** "ceiling fan with remote" is Atomberg's weakest keyword: it still leads with 22.9%, but Crompton is close at 20.2%. Defend it with comparison videos and SEO-optimised titles.
5. **Lead with what people praise.** "energy saving" drives the most praise: 147 of 595 positive Atomberg mentions. Make it the first claim in ads and creator briefs.
6. **Top complaint theme.** "noise" is the top complaint: 52 of 268 negative Atomberg mentions. Action: lead creatives with silent-operation demos recorded on camera.
7. **Channels already talking about you.** Top channels mentioning Atomberg in high-ranked results: Channel Papa, Channel Lima, Channel Bravo. Offer them early access to launches.
8. **Top-10 videos that skip you.** 3 top-10 videos mention competitors but not Atomberg. Highest-view channels to approach: Channel Papa, Channel Foxtrot, Channel India.
