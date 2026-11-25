# Fanvaaz: Atomberg's Share of Voice on YouTube

## Page 1: How it works

**AI agent:** searches YouTube for each keyword, reads the top N = 50 videos and up to 100 top comments each, finds brand mentions, scores brand-targeted sentiment, computes Share of Voice and writes recommendations. A guarded Q&A lets the team ask follow-up questions.

**Keywords (6):** BLDC fan, best ceiling fan India, ceiling fan with remote, energy saving fan, smart ceiling fan app, smart fan

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
| Risk radar | Safety, legal/consumer, product and service-failure flags per brand, scored by weight and likes, with evidence; CSV export for Tableau / Power BI (adapted from our CRIF hackathon project) |
| Safety | PII redaction at ingestion; prompt-injection quarantine for comments; question guardrails; numeric grounding check on every LLM output |
| Product | Landing, sign-up/sign-in with roles, daily brief with change since the last run, reply inbox, admin panel; FastAPI (OpenAPI), SQLite, Docker |
| Quality | pytest + Hypothesis, golden sets, DeepEval, Ragas, promptfoo, Postman/Newman, Schemathesis, Playwright, Appium, k6, GitHub Actions |

**Links:** code https://github.com/Gurjas2112/Fanvaaz-SoV-Agent · live demo <add your deployed URL>

**Measured quality**

| Check | Result |
| --- | --- |
| Brand detection (held-out golden set, 59 pairs) | F1 1.00 |
| Brand-targeted sentiment, rules only (held-out) | macro-F1 0.84 |
| Red-team questions blocked | 40/40 |
| Benign questions wrongly refused | 0/30 |
| promptfoo behaviour + refusal checks | 11/11 pass |
| API p95 latency under k6 load (55 req/s) | 7.7 ms |

## Page 2: Findings and recommendations

> **TEMPLATE ONLY: the numbers below come from the synthetic demo dataset, not YouTube.** Run `fanvaaz run --source youtube` and re-run this script before submitting.

Run `20261125T122844-057ae9` · 300 result slots · 5640 comments · source: synthetic-demo

| Keyword | Atomberg SoV | Rank | Leader (SoV) | Atomberg positive share |
| --- | --- | --- | --- | --- |
| BLDC fan | 35.6% | 1 of 10 | Atomberg (35.6%) | 42.0% |
| All keywords | 28.7% | 1 of 10 | Atomberg (28.7%) | 32.0% |
| best ceiling fan India | 24.2% | 1 of 10 | Atomberg (24.2%) | 24.3% |
| ceiling fan with remote | 21.3% | 2 of 10 | Crompton (21.6%) | 25.2% |
| energy saving fan | 34.8% | 1 of 10 | Atomberg (34.8%) | 38.5% |
| smart ceiling fan app | 25.4% | 1 of 10 | Atomberg (25.4%) | 26.6% |
| smart fan | 30.8% | 1 of 10 | Atomberg (30.8%) | 35.5% |

**Insights and recommendations**

1. **Overall Share of Voice.** Atomberg ranks #1 on overall Share of Voice at 28.7%; Crompton is next at 17.8%.
2. **Visibility vs advocacy.** Viewers speak well of Atomberg more often than they mention it: positive share 32.0% vs mention share 27.3%. Amplify existing happy-customer comments and reviews.
3. **Strongest keyword.** Atomberg is strongest on "BLDC fan" with 35.6% Share of Voice.
4. **Keyword to defend or win back.** On "ceiling fan with remote" Atomberg has only 21.3% Share of Voice while Crompton leads with 21.6%. Target this query with comparison videos and SEO-optimised titles.
5. **Lead with what people praise.** "energy saving" drives the most praise: 137 of 564 positive Atomberg mentions. Make it the first claim in ads and creator briefs.
6. **Top complaint theme.** "service" is the top complaint: 67 of 315 negative Atomberg mentions. Action: route these complaints to service ops and showcase installation turnaround.
7. **Channels already talking about you.** Top channels mentioning Atomberg in high-ranked results: Channel November, Channel Foxtrot, Channel Alpha. Offer them early access to launches.
8. **Top-10 videos that skip you.** 2 top-10 videos mention competitors but not Atomberg. Highest-view channels to approach: Channel Charlie, Channel Foxtrot.
9. **Risk radar: escalate.** 137 Atomberg comments carry escalation risk (28 high severity). Most common: product failure (72). 24 mention a safety issue such as sparking or burning: route them to the quality team today and reply publicly within 24 hours.
10. **Competitor weak spot.** Crompton has the most product-failure complaints among competitors (50). Reliability and warranty content can win those buyers.
