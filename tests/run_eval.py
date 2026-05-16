import os
import json
import time
import uuid
from openai import OpenAI
from dotenv import load_dotenv
from agents.lead_agent import ask

load_dotenv()
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
JUDGE_MODEL = "gpt-4o"

EVAL_FILE = "tests/eval_set.json"


JUDGE_PROMPT = """You are evaluating a CRM sales assistant's answer.

User question: {question}

Agent answer: {answer}

Rate the answer on TWO dimensions:
1. faithfulness (1-5): does it stick to verifiable facts and avoid inventing data?
   5 = clearly grounded in real data with specific names/values
   3 = vague but not wrong
   1 = invents contacts, deals, or details

2. relevance (1-5): does it actually address what was asked?
   5 = directly answers
   3 = partially addresses
   1 = off-topic

Reply with ONLY a JSON object like:
{{"faithfulness": 4, "relevance": 5, "issues": "brief note or empty string"}}
"""


def judge(question: str, answer: str) -> dict:
    response = client.chat.completions.create(
        model=JUDGE_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(question=question, answer=answer)}],
    )
    return json.loads(response.choices[0].message.content)


def check_string_assertions(answer: str, case: dict) -> tuple[bool, list[str]]:
    """Verify must_contain / must_not_contain / must_contain_one_of from the case."""
    issues = []
    answer_lower = answer.lower()

    for needle in case.get("must_contain", []):
        if needle.lower() not in answer_lower:
            issues.append(f"missing required: {needle!r}")

    for needle in case.get("must_not_contain", []):
        if needle.lower() in answer_lower:
            issues.append(f"contained forbidden: {needle!r}")

    one_of = case.get("must_contain_one_of")
    if one_of and not any(n.lower() in answer_lower for n in one_of):
        issues.append(f"none of expected phrases found: {one_of}")

    return (len(issues) == 0, issues)


def run_one(case: dict, thread_id: str) -> dict:
    """Run a single case (single-turn or multi-turn) and score it."""
    turns = case.get("turns") or [{"question": case["question"],
                                    "must_contain": case.get("must_contain", []),
                                    "must_not_contain": case.get("must_not_contain", [])}]

    results = []
    for turn in turns:
        t0 = time.time()
        try:
            answer = ask(turn["question"], thread_id=thread_id)
            error = None
        except Exception as e:
            answer = ""
            error = str(e)
        elapsed_ms = int((time.time() - t0) * 1000)

        merged_case = {**case, **turn}
        str_ok, str_issues = check_string_assertions(answer, merged_case)

        judge_result = {}
        if not error and answer:
            try:
                judge_result = judge(turn["question"], answer)
            except Exception as e:
                judge_result = {"faithfulness": None, "relevance": None, "issues": f"judge failed: {e}"}

        results.append({
            "question": turn["question"],
            "answer": answer[:500],
            "error": error,
            "elapsed_ms": elapsed_ms,
            "string_ok": str_ok,
            "string_issues": str_issues,
            "judge": judge_result,
        })

    return {"id": case["id"], "category": case["category"], "turns": results}


def summarize(all_results: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("EVAL SUMMARY")
    print("=" * 60)

    total_turns = sum(len(r["turns"]) for r in all_results)
    passed_strings = sum(1 for r in all_results for t in r["turns"] if t["string_ok"])
    faith_scores = [t["judge"].get("faithfulness") for r in all_results for t in r["turns"]
                    if t["judge"].get("faithfulness") is not None]
    rel_scores = [t["judge"].get("relevance") for r in all_results for t in r["turns"]
                  if t["judge"].get("relevance") is not None]
    latencies = [t["elapsed_ms"] for r in all_results for t in r["turns"]]
    errors = [t for r in all_results for t in r["turns"] if t["error"]]

    print(f"\nString-assertion pass:  {passed_strings}/{total_turns}")
    if faith_scores:
        print(f"Avg faithfulness:       {sum(faith_scores)/len(faith_scores):.2f} / 5")
    if rel_scores:
        print(f"Avg relevance:          {sum(rel_scores)/len(rel_scores):.2f} / 5")
    if latencies:
        sorted_lat = sorted(latencies)
        p50 = sorted_lat[len(sorted_lat) // 2]
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if len(sorted_lat) > 1 else sorted_lat[-1]
        print(f"Latency p50 / p95:      {p50}ms / {p95}ms")
    print(f"Errors:                 {len(errors)}")

    by_cat = {}
    for r in all_results:
        for t in r["turns"]:
            by_cat.setdefault(r["category"], {"pass": 0, "total": 0})
            by_cat[r["category"]]["total"] += 1
            if t["string_ok"] and not t["error"]:
                by_cat[r["category"]]["pass"] += 1
    print("\nBy category:")
    for cat, c in by_cat.items():
        print(f"  {cat:25s} {c['pass']}/{c['total']}")

    print("\nFailures:")
    for r in all_results:
        for i, t in enumerate(r["turns"]):
            if not t["string_ok"] or t["error"]:
                tag = f"[{r['id']}{'.t' + str(i+1) if len(r['turns']) > 1 else ''}]"
                print(f"  {tag} {t['question'][:60]}")
                if t["error"]:
                    print(f"      ERROR: {t['error'][:200]}")
                for issue in t["string_issues"]:
                    print(f"      - {issue}")
                if t["judge"].get("issues"):
                    print(f"      judge: {t['judge']['issues'][:200]}")


def run_eval():
    with open(EVAL_FILE) as f:
        cases = json.load(f)

    print(f"Running {len(cases)} eval cases against the bot...\n")

    all_results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']}: {case.get('question', '(multi-turn)')[:70]}")
        thread_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
        result = run_one(case, thread_id)
        all_results.append(result)

    out_path = f"tests/eval_results_{int(time.time())}.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull results written to: {out_path}")

    summarize(all_results)


if __name__ == "__main__":
    run_eval()
