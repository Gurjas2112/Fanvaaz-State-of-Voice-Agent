"""Observability: structured logs, spans, the LLM ledger and the AI Ops numbers.

* Logs: one JSON object per line when FANVAAZ_LOG_JSON=true (Cloud Logging parses
  severity, trace and fields from it); plain text on a laptop.
* Spans: every LangGraph node runs inside `span(graph, node)`. Each span is a row
  in `spans` (trace id, node, duration, status) and a log line.
* Ledger: every LLM call (sent, cached or refused) is a row in `llm_calls` with
  prompt version, estimated and actual tokens, cached tokens, cost, latency,
  cache hit and schema validity.
* Metrics: `ai_ops()` aggregates the ledger for the admin panel; `prometheus()`
  renders the same numbers in Prometheus text format for /metrics.

The sink (the store) is bound once at startup. Unbound, every call is a no-op,
so library code never needs to know whether observability is on.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("fanvaaz.obs")

_sink = None  # a Store
_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("fanvaaz_trace", default=None)
_run: contextvars.ContextVar[str | None] = contextvars.ContextVar("fanvaaz_run", default=None)


def bind(store) -> None:
    global _sink
    _sink = store


def bound():
    return _sink


# ---- logging -------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        }
        trace = _trace.get()
        if trace:
            out["trace_id"] = trace
        run = _run.get()
        if run:
            out["run_id"] = run
        for key in ("node", "graph", "duration_ms", "status", "tokens", "cost_usd"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging(json_logs: bool = False, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)  # stderr: stdout belongs to MCP stdio when used
    handler.setFormatter(
        JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


# ---- traces and spans ------------------------------------------------------------
def new_trace(run_id: str | None = None) -> str:
    tid = uuid.uuid4().hex[:16]
    _trace.set(tid)
    _run.set(run_id)
    return tid


def current_trace() -> str | None:
    return _trace.get()


def current_run() -> str | None:
    return _run.get()


def set_run(run_id: str | None) -> None:
    _run.set(run_id)


@contextmanager
def span(graph: str, node: str, **detail):
    trace = _trace.get() or new_trace()
    t0 = time.perf_counter()
    started = datetime.now(UTC).isoformat(timespec="milliseconds")
    status = "ok"
    try:
        yield detail
    except Exception:
        status = "error"
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        log.info(
            "%s.%s %s in %.0f ms",
            graph,
            node,
            status,
            ms,
            extra={"graph": graph, "node": node, "duration_ms": round(ms, 1), "status": status},
        )
        if _sink is not None:
            try:
                with _sink._conn() as c:
                    c.execute(
                        "INSERT INTO spans (trace_id, graph, node, started_at, duration_ms, status, detail)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (trace, graph, node, started, ms, status, json.dumps(detail, default=str)[:2000]),
                    )
            except Exception:  # noqa: BLE001 - telemetry never breaks the request
                log.debug("span write failed", exc_info=True)


def trace_spans(trace_id: str) -> list[dict]:
    if _sink is None:
        return []
    with _sink._conn() as c:
        rows = c.execute(
            "SELECT graph, node, started_at, duration_ms, status, detail FROM spans WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        ).fetchall()
    return [
        {
            "graph": r[0],
            "node": r[1],
            "started_at": r[2],
            "duration_ms": round(r[3], 1),
            "status": r[4],
            "detail": json.loads(r[5] or "{}"),
        }
        for r in rows
    ]


# ---- LLM ledger -------------------------------------------------------------------
@dataclass
class CallRecord:
    node: str
    model: str
    prompt_id: str
    prompt_version: str
    est_prompt_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cache_hit: bool = False
    schema_ok: bool | None = None
    outcome: str = "ok"  # ok | cached | refused:<reason> | error | invalid


def record_call(rec: CallRecord) -> None:
    log.info(
        "llm %s %s %s tokens=%d+%d cached=%d cost=$%.6f",
        rec.node,
        rec.prompt_id,
        rec.outcome,
        rec.prompt_tokens,
        rec.completion_tokens,
        rec.cached_tokens,
        rec.cost_usd,
        extra={
            "node": rec.node,
            "tokens": rec.prompt_tokens + rec.completion_tokens,
            "cost_usd": rec.cost_usd,
        },
    )
    if _sink is None:
        return
    try:
        with _sink._conn() as c:
            c.execute(
                "INSERT INTO llm_calls (ts, run_id, node, model, prompt_id, prompt_version, est_prompt_tokens,"
                " prompt_tokens, completion_tokens, cached_tokens, cost_usd, latency_ms, cache_hit, schema_ok, outcome)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    _run.get(),
                    rec.node,
                    rec.model,
                    rec.prompt_id,
                    rec.prompt_version,
                    rec.est_prompt_tokens,
                    rec.prompt_tokens,
                    rec.completion_tokens,
                    rec.cached_tokens,
                    rec.cost_usd,
                    rec.latency_ms,
                    int(rec.cache_hit),
                    None if rec.schema_ok is None else int(rec.schema_ok),
                    rec.outcome[:120],
                ),
            )
    except Exception:  # noqa: BLE001
        log.debug("ledger write failed", exc_info=True)


# ---- aggregates ----------------------------------------------------------------------
def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)


def ai_ops(store, days: int = 7) -> dict:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with store._conn() as c:
        rows = c.execute(
            "SELECT node, prompt_id, prompt_version, est_prompt_tokens, prompt_tokens, completion_tokens,"
            " cached_tokens, cost_usd, latency_ms, cache_hit, schema_ok, outcome, run_id, ts, model"
            " FROM llm_calls WHERE ts >= ? ORDER BY id",
            (since,),
        ).fetchall()
        span_rows = c.execute(
            "SELECT graph, node, duration_ms, status FROM spans WHERE started_at >= ?", (since,)
        ).fetchall()
    sent = [r for r in rows if r[11] in ("ok", "invalid", "error")]
    cached = [r for r in rows if r[9]]
    refused = [r for r in rows if str(r[11]).startswith("refused")]
    schema_checked = [r for r in rows if r[10] is not None and not r[9]]
    by_node: dict[str, dict] = {}
    for r in rows:
        d = by_node.setdefault(
            r[0],
            {
                "calls": 0,
                "cached": 0,
                "refused": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "cost_usd": 0.0,
                "est_prompt_tokens": 0,
                "latencies": [],
            },
        )
        d["calls"] += 1
        d["cached"] += int(bool(r[9]))
        d["refused"] += int(str(r[11]).startswith("refused"))
        d["prompt_tokens"] += r[4]
        d["completion_tokens"] += r[5]
        d["cached_tokens"] += r[6]
        d["cost_usd"] += r[7]
        if not r[9] and r[11] in ("ok", "invalid"):
            d["est_prompt_tokens"] += r[3]
            d["latencies"].append(r[8])
    for d in by_node.values():
        lat = d.pop("latencies")
        d["p50_ms"], d["p95_ms"] = _pct(lat, 0.5), _pct(lat, 0.95)
        d["cost_usd"] = round(d["cost_usd"], 6)
    by_run: dict[str, float] = {}
    for r in rows:
        if r[12]:
            by_run[r[12]] = by_run.get(r[12], 0.0) + r[7]
    nodes: dict[str, list[float]] = {}
    errors = 0
    for g, n, ms, st in span_rows:
        nodes.setdefault(f"{g}.{n}", []).append(ms)
        errors += st == "error"
    actual = sum(r[4] for r in sent)
    est = sum(r[3] for r in sent)
    return {
        "window_days": days,
        "calls": {"total": len(rows), "sent": len(sent), "cached": len(cached), "refused": len(refused)},
        "tokens": {
            "prompt": sum(r[4] for r in rows),
            "completion": sum(r[5] for r in rows),
            "cached_prompt": sum(r[6] for r in rows),
            "estimated_prompt": est,
            "estimate_error_pct": round(100 * (est - actual) / actual, 1) if actual else None,
        },
        "cost_usd": {
            "total": round(sum(r[7] for r in rows), 6),
            "by_run": {k: round(v, 6) for k, v in by_run.items()},
        },
        "cache_hit_rate": round(len(cached) / len(rows), 3) if rows else None,
        "provider_cache_rate": round(sum(r[6] for r in sent) / actual, 3) if actual else None,
        "schema_valid_rate": (
            round(sum(1 for r in schema_checked if r[10]) / len(schema_checked), 3)
            if schema_checked
            else None
        ),
        "refusal_reasons": sorted({str(r[11]) for r in refused}),
        "by_node": by_node,
        "spans": {
            k: {"count": len(v), "p50_ms": _pct(v, 0.5), "p95_ms": _pct(v, 0.95)}
            for k, v in sorted(nodes.items())
        },
        "span_errors": errors,
        "recent": [
            {
                "ts": r[13],
                "node": r[0],
                "prompt": f"{r[1]}@{r[2]}",
                "model": r[14],
                "outcome": r[11],
                "tokens": r[4] + r[5],
                "cached_tokens": r[6],
                "cost_usd": round(r[7], 6),
                "latency_ms": round(r[8], 0),
                "cache_hit": bool(r[9]),
            }
            for r in rows[-25:][::-1]
        ],
    }


def prometheus(store) -> str:
    ops = ai_ops(store, days=1)
    lines = [
        "# HELP fanvaaz_llm_calls_total LLM calls in the last 24h by outcome",
        "# TYPE fanvaaz_llm_calls_total gauge",
    ]
    for k, v in ops["calls"].items():
        lines.append(f'fanvaaz_llm_calls_total{{outcome="{k}"}} {v}')
    lines += [
        "# HELP fanvaaz_llm_tokens LLM tokens in the last 24h",
        "# TYPE fanvaaz_llm_tokens gauge",
    ]
    for k in ("prompt", "completion", "cached_prompt", "estimated_prompt"):
        lines.append(f'fanvaaz_llm_tokens{{kind="{k}"}} {ops["tokens"][k]}')
    lines += [
        "# HELP fanvaaz_llm_cost_usd LLM spend in the last 24h",
        "# TYPE fanvaaz_llm_cost_usd gauge",
        f"fanvaaz_llm_cost_usd {ops['cost_usd']['total']}",
        "# HELP fanvaaz_llm_schema_valid_ratio Share of LLM replies that validated",
        "# TYPE fanvaaz_llm_schema_valid_ratio gauge",
        f"fanvaaz_llm_schema_valid_ratio {ops['schema_valid_rate'] if ops['schema_valid_rate'] is not None else 'NaN'}",
        "# HELP fanvaaz_node_latency_ms Graph node latency p95 in the last 24h",
        "# TYPE fanvaaz_node_latency_ms gauge",
    ]
    for node, v in ops["spans"].items():
        lines.append(f'fanvaaz_node_latency_ms{{node="{node}",q="p95"}} {v["p95_ms"]}')
    lines.append(f"fanvaaz_span_errors {ops['span_errors']}")
    return "\n".join(lines) + "\n"
