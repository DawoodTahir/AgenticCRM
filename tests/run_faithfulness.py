"""
Faithfulness audit — runs each case in faithfulness_set.json and checks
that the bot:
  1. Includes facts that MUST be present (must_contain / must_contain_one_of)
  2. NEVER includes hallucinations (must_not_contain / must_not_contain_regex)

Outputs:
  - Per-case PASS / FAIL with what went wrong
  - Summary: pass rate, hallucination count, missing-fact count
  - Timestamped JSON of full results for diffing across runs
"""
import os
import re
import sys
import json
import time
import uuid
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.lead_agent import ask

CASES_FILE = "tests/faithfulness_set.json"


def check_case(answer: str, case: dict) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failure_reasons)."""
    failures = []
    a = (answer or "").lower()

    for needle in case.get("must_contain", []):
        if needle.lower() not in a:
            failures.append(f"MISSING required fact: {needle!r}")

    one_of = case.get("must_contain_one_of")
    if one_of and not any(n.lower() in a for n in one_of):
        failures.append(f"NONE of expected facts present: {one_of}")

    one_of_re = case.get("must_contain_one_of_regex")
    if one_of_re and not any(re.search(p, answer or "", re.IGNORECASE) for p in one_of_re):
        failures.append(f"NONE of expected regex patterns matched: {one_of_re}")

    for needle in case.get("must_not_contain", []):
        if needle.lower() in a:
            failures.append(f"HALLUCINATION — contained forbidden: {needle!r}")

    for pattern in case.get("must_not_contain_regex", []):
        if re.search(pattern, answer or "", re.IGNORECASE):
            failures.append(f"HALLUCINATION — matched regex: {pattern!r}")

    return (len(failures) == 0, failures)


def run():
    with open(CASES_FILE) as f:
        cases = json.load(f)

    run_id = int(time.time())
    out_path = f"tests/faithfulness_results_{run_id}.json"
    print(f"Faithfulness audit — {len(cases)} cases\n")

    results = []
    pass_count = 0
    hallucination_count = 0
    missing_fact_count = 0

    for i, case in enumerate(cases, 1):
        cid = case["id"]
        q = case["question"]
        thread_id = f"faith-{cid}-{run_id}-{uuid.uuid4().hex[:6]}"

        print(f"[{i}/{len(cases)}] {cid}  {case.get('category','')}  {q[:60]}...", flush=True)
        t0 = time.time()
        try:
            answer = ask(q, thread_id=thread_id)
            error = None
        except Exception as e:
            answer = ""
            error = str(e)
            print(f"    ! exception: {error[:200]}")
        elapsed_ms = int((time.time() - t0) * 1000)

        passed, failures = (False, [f"EXCEPTION: {error}"]) if error else check_case(answer, case)

        for fail in failures:
            if fail.startswith("HALLUCINATION"):
                hallucination_count += 1
            elif fail.startswith(("MISSING", "NONE")):
                missing_fact_count += 1

        tag = "PASS" if passed else "FAIL"
        print(f"    {tag}  ({elapsed_ms}ms)")
        for fail in failures:
            print(f"      - {fail}")

        if passed:
            pass_count += 1

        results.append({
            "id": cid,
            "category": case.get("category"),
            "question": q,
            "answer": answer[:1500],
            "elapsed_ms": elapsed_ms,
            "passed": passed,
            "failures": failures,
            "error": error,
        })

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print(f"FAITHFULNESS SUMMARY  ({run_id})")
    print("=" * 70)
    print(f"Cases run:                 {len(cases)}")
    print(f"PASS:                      {pass_count} / {len(cases)} ({pass_count*100//len(cases)}%)")
    print(f"Hallucinations detected:   {hallucination_count}")
    print(f"Missing-fact failures:     {missing_fact_count}")
    print(f"Full results:              {out_path}")

    # By category breakdown
    by_cat = {}
    for r in results:
        cat = r["category"] or "uncategorized"
        by_cat.setdefault(cat, {"pass": 0, "total": 0})
        by_cat[cat]["total"] += 1
        if r["passed"]:
            by_cat[cat]["pass"] += 1
    print("\nBy category:")
    for cat, c in sorted(by_cat.items()):
        print(f"  {cat:30s} {c['pass']}/{c['total']}")


if __name__ == "__main__":
    run()
