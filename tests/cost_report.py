"""
Cost & latency report — parses logs/crm.log and aggregates token usage
into per-query stats so you can see:
  - Average $ per query
  - Most expensive queries
  - Token breakdown by model (agent vs critic)
  - Latency p50 / p95 / p99
  - Trend: queries that triggered multiple critic retries

Assumes the structured-log format produced by lead_agent.py:
  - {"event": "agent_input",  "question": "...", "thread_id": "..."}
  - {"event": "llm_call",     "model": "...", "role": "...", "elapsed_ms": N,
                              "input_tokens": N, "output_tokens": N}
  - {"event": "agent_output", "thread_id": "...", "total_elapsed_ms": N,
                              "iterations": N}

A "query" is one agent_input → agent_output pair on the same thread_id.
"""
import json
import os
import re
import sys
from collections import defaultdict
from statistics import mean, median

LOG_PATH = "logs/crm.log"

# Pricing (USD per 1M tokens) — update as OpenAI changes prices
PRICING = {
    "gpt-4o":              {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":         {"input": 0.15,  "output":  0.60},
    "gpt-4-turbo":         {"input": 10.00, "output": 30.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.00},
}


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model, {"input": 0, "output": 0})
    return (input_tokens or 0) * p["input"] / 1_000_000 + \
           (output_tokens or 0) * p["output"] / 1_000_000


def parse_log(path: str = LOG_PATH) -> list[dict]:
    """Read every JSON log line; return list of dicts."""
    events = []
    if not os.path.exists(path):
        print(f"Log file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def group_into_queries(events: list[dict]) -> list[dict]:
    """
    Walk events in order. Each agent_input opens a query bucket
    (keyed by thread_id). LLM calls accumulate into the current bucket
    for that thread. agent_output closes it.
    """
    open_query = {}  # thread_id -> dict
    queries = []

    for ev in events:
        evt = ev.get("event")
        tid = ev.get("thread_id")

        if evt == "agent_input":
            if tid in open_query:
                queries.append(open_query.pop(tid))
            open_query[tid] = {
                "thread_id": tid,
                "question": ev.get("question", ""),
                "ts_start": ev.get("timestamp"),
                "llm_calls": [],
                "iterations": 0,
                "total_elapsed_ms": None,
            }

        elif evt == "llm_call" and tid is None:
            # llm_call events don't always have a thread_id — attach to
            # the most recently opened query (could be wrong if interleaved).
            if open_query:
                # pick the most recently opened
                latest_tid = list(open_query.keys())[-1]
                open_query[latest_tid]["llm_calls"].append(ev)

        elif evt == "llm_call" and tid in open_query:
            open_query[tid]["llm_calls"].append(ev)

        elif evt == "agent_output" and tid in open_query:
            q = open_query.pop(tid)
            q["iterations"] = ev.get("iterations", 0)
            q["total_elapsed_ms"] = ev.get("total_elapsed_ms")
            q["ts_end"] = ev.get("timestamp")
            queries.append(q)

    # Close any orphaned queries
    queries.extend(open_query.values())

    # Compute totals
    for q in queries:
        in_t = sum(c.get("input_tokens") or 0 for c in q["llm_calls"])
        out_t = sum(c.get("output_tokens") or 0 for c in q["llm_calls"])
        q["input_tokens"] = in_t
        q["output_tokens"] = out_t
        q["cost_usd"] = sum(
            _cost(c.get("model", ""), c.get("input_tokens"), c.get("output_tokens"))
            for c in q["llm_calls"]
        )
        q["llm_call_count"] = len(q["llm_calls"])
    return queries


def percentile(values: list, p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(int(len(s) * p), len(s) - 1)
    return s[i]


def report(queries: list[dict]) -> None:
    if not queries:
        print("No queries found in log.")
        return

    print(f"\n{'=' * 70}")
    print(f"COST & LATENCY REPORT  —  {len(queries)} queries analyzed")
    print("=" * 70)

    costs = [q["cost_usd"] for q in queries]
    latencies = [q["total_elapsed_ms"] for q in queries if q["total_elapsed_ms"]]
    iterations = [q["iterations"] for q in queries]
    in_tokens = [q["input_tokens"] for q in queries]
    out_tokens = [q["output_tokens"] for q in queries]

    print(f"\nCost per query:")
    print(f"  Mean:    ${mean(costs):.4f}")
    print(f"  Median:  ${median(costs):.4f}")
    print(f"  p95:     ${percentile(costs, 0.95):.4f}")
    print(f"  Total:   ${sum(costs):.2f}  (across all queries)")

    if latencies:
        print(f"\nLatency (end-to-end):")
        print(f"  p50:     {int(median(latencies))}ms")
        print(f"  p95:     {int(percentile(latencies, 0.95))}ms")
        print(f"  p99:     {int(percentile(latencies, 0.99))}ms")
        print(f"  Max:     {max(latencies)}ms")

    print(f"\nTokens per query:")
    print(f"  Mean input:   {int(mean(in_tokens))}")
    print(f"  Mean output:  {int(mean(out_tokens))}")
    print(f"  Mean ratio:   {mean(in_tokens)/max(mean(out_tokens), 1):.1f}× input/output")

    print(f"\nCritic loop:")
    no_retry = sum(1 for i in iterations if i <= 1)
    retried = sum(1 for i in iterations if i > 1)
    max_iter = sum(1 for i in iterations if i >= 4)
    print(f"  Single-pass:        {no_retry} ({no_retry*100//len(queries)}%)")
    print(f"  Retried at least 1x: {retried}")
    print(f"  Hit max iterations:  {max_iter}")

    # By model
    by_model = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
    for q in queries:
        for c in q["llm_calls"]:
            m = c.get("model", "?")
            by_model[m]["calls"] += 1
            by_model[m]["input"] += c.get("input_tokens") or 0
            by_model[m]["output"] += c.get("output_tokens") or 0
            by_model[m]["cost"] += _cost(m, c.get("input_tokens"), c.get("output_tokens"))

    print(f"\nBy model:")
    print(f"  {'Model':22s} {'Calls':>8s} {'Input':>10s} {'Output':>10s} {'Cost':>10s}")
    for m, s in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {m:22s} {s['calls']:>8d} {s['input']:>10d} {s['output']:>10d} {'$'+format(s['cost'], '.4f'):>10s}")

    # Top expensive queries
    print(f"\nTop 10 most expensive queries:")
    print(f"  {'$':>8s} {'iters':>6s} {'in_tok':>8s} {'out_tok':>8s}  question")
    for q in sorted(queries, key=lambda x: -x["cost_usd"])[:10]:
        qtext = (q["question"] or "")[:70].replace("\n", " ")
        print(f"  ${q['cost_usd']:>6.4f} {q['iterations']:>6d} "
              f"{q['input_tokens']:>8d} {q['output_tokens']:>8d}  {qtext}")

    # Slowest
    if latencies:
        print(f"\nTop 5 slowest queries:")
        for q in sorted([x for x in queries if x["total_elapsed_ms"]],
                        key=lambda x: -x["total_elapsed_ms"])[:5]:
            qtext = (q["question"] or "")[:70].replace("\n", " ")
            print(f"  {q['total_elapsed_ms']:>6d}ms  iters={q['iterations']}  ${q['cost_usd']:.4f}  {qtext}")


if __name__ == "__main__":
    log_path = sys.argv[1] if len(sys.argv) > 1 else LOG_PATH
    events = parse_log(log_path)
    queries = group_into_queries(events)
    report(queries)
