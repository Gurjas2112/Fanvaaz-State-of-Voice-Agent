# Fanvaaz AI system card

What the AI parts of Fanvaaz do, what they may not do, how they are measured, and how
they fail. The enforceable version of this card is
[`fanvaaz/data/policy.yaml`](../fanvaaz/data/policy.yaml), applied by
[`fanvaaz/governance.py`](../fanvaaz/governance.py) and the LLM gateway in
[`fanvaaz/ai.py`](../fanvaaz/ai.py).

## Purpose and users

Marketing and content teams track their brand's share of the YouTube conversation against
rivals, see which comments need a reply, and ask questions about what viewers say. Output
informs content and outreach decisions. It is not used to make decisions about individuals.

## Data

| Item | Handling |
| --- | --- |
| Source | YouTube Data API v3: `search.list`, `videos.list`, `commentThreads.list` (public data) |
| Personal data | Emails and Indian phone numbers are redacted at ingestion; commenter ids are SHA-256 hashed; author names are never stored |
| Retention | Raw API responses are off in production (`FANVAAZ_SAVE_RAW=false`); conversation memory expires after 24 h, preferences after 30 days |
| Test data | A recorded, redacted real collection (99 videos, 3,555 comments), not synthetic data |

## Where a model is used, and the guard on each

| Task (policy id) | Model | Guard | On failure |
| --- | --- | --- | --- |
| `sentiment`: re-check uncertain rule labels | Gemini Flash | Schema `SentimentBatchOut`; may label only brands the deterministic matcher found | Rule label kept |
| `rewrite`: polish insight wording | Gemini Flash | Schema `RewriteOut`; every number must be in the insight's evidence | Rule text kept |
| `ask`: answer a question | Gemini Flash | Schema `AnswerOut`; numbers grounded in stats or quoted comments; citations must be ids it was given; output injection check; PII and secret redaction | Extractive answer |
| `plan`: split a question | Gemini Flash | Only brands and aspects from the profile are kept | Rule planner |
| embeddings for retrieval | `gemini-embedding-001` | Injection-like comments are never embedded | Lexical (BM25) retrieval only |

Brands are never detected by a model, so a comment cannot inject a fake brand mention.

## Controls

- Kill switch: `FANVAAZ_LLM_ENABLED=false` turns every model step off; the product keeps working on rules.
- Model allow-list, task allow-list, context budget (12,000 prompt tokens) and cost caps per run and per day.
- Circuit breaker: an auth or billing error (401/402/403) stops all model calls for the process.
- Human in the loop: tracking-profile changes and collections are admin-only and written to the audit log.
- Every model call is in the LLM ledger: prompt id/version/hash, estimated and reported tokens, cached tokens, cost, latency, schema validity and outcome.

## Measured quality (recorded real data; see the README for how)

| Check | Result |
| --- | --- |
| Brand-targeted sentiment, rules only, REAL comments (untouched holdout) | macro-F1 0.73 |
| Same, hand-written holdout | macro-F1 0.86 |
| Retrieval on 8 real questions, re-ranked vs plain BM25 | MRR 0.84 vs 0.44; recall@5 0.73 vs 0.25 |
| Red-team questions blocked / benign questions refused | 40/40 / 0/30 |
| Real comments wrongly quarantined as injections | 0 / 3,555 |
| Insights whose numbers are grounded and citations resolve | 100% |

## Known limits

- Slang that reverses meaning ("this phone is fire") is read literally by the rules (a tracked, expected failure).
- The real-comment golden sets are small (about 60 brand pairs each) and labelled by one person, so the error bars are wide.
- Retrieval judgments are pooled from the systems compared, so absolute recall is an upper bound.
- LLM-graded metrics (DeepEval, Ragas) need a funded model key and are reported only when they have actually run.
- Rate limits and login throttling are per process; with several instances they become per instance.
