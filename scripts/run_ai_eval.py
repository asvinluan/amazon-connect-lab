"""Run the AI assist evaluation set against the deployed Lambda.

Sends every prompt in evals/guardrail_cases.json to the function, compares the
returned status with the expected one, prints a table and a pass rate, and
writes evals/results.json. The results file holds no secrets: case ids,
statuses, latency and a short preview of the answer.

Usage (PowerShell, from the repository root):

    python scripts/run_ai_eval.py --function asvinluan-connect-lab-ai-assist

Needs boto3 and AWS credentials, the same ones Terraform uses.
Exit code 0 means every case passed, 1 means at least one did not.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = ROOT / "evals" / "guardrail_cases.json"
RESULTS_FILE = ROOT / "evals" / "results.json"
PREVIEW_CHARS = 100


def load_cases(path: Path = CASES_FILE) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def judge(case: dict[str, Any], reply: dict[str, str]) -> tuple[bool, str]:
    """Pass if the status matches. Grounding cases must also decline or cite an agent."""
    got = reply.get("status", "")
    if got != case["expect"]:
        return False, f"expected {case['expect']}, got {got} ({reply.get('reason', '')})"
    needles = case.get("contains_any")
    if needles and case["expect"] == "answered":
        answer = reply.get("answer", "").lower()
        if not any(n.lower() in answer for n in needles):
            return False, "answered, but the text did not match the expected wording"
    return True, ""


def evaluate(cases: list[dict[str, Any]], ask: Callable[[str], dict[str, str]]) -> dict[str, Any]:
    rows = []
    for case in cases:
        started = time.monotonic()
        reply = ask(case["prompt"])
        ok, note = judge(case, reply)
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expect": case["expect"],
                "got": reply.get("status", ""),
                "reason": reply.get("reason", ""),
                "passed": ok,
                "note": note,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "answer_preview": reply.get("answer", "")[:PREVIEW_CHARS],
            }
        )
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        by_category[row["category"]][1] += 1
        by_category[row["category"]][0] += int(row["passed"])
    passed = sum(1 for r in rows if r["passed"])
    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # noqa: UP017
        "total": len(rows),
        "passed": passed,
        "pass_rate": round(passed / len(rows), 3) if rows else 0.0,
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in by_category.items()},
        "cases": rows,
    }


def _lambda_asker(function_name: str, region: str) -> Callable[[str], dict[str, str]]:
    import boto3  # imported here so the tests do not need AWS configured

    client = boto3.client("lambda", region_name=region)

    def ask(question: str) -> dict[str, str]:
        out = client.invoke(
            FunctionName=function_name,
            Payload=json.dumps({"question": question}).encode(),
        )
        return json.loads(out["Payload"].read())

    return ask


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--function", required=True, help="Lambda function name")
    parser.add_argument("--region", default="ap-southeast-2")
    args = parser.parse_args()

    report = evaluate(load_cases(), _lambda_asker(args.function, args.region))
    RESULTS_FILE.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"{'case':<14}{'expect':<10}{'got':<10}{'ms':>6}  result")
    for row in report["cases"]:
        mark = "PASS" if row["passed"] else "FAIL  " + row["note"]
        print(f"{row['id']:<14}{row['expect']:<10}{row['got']:<10}{row['latency_ms']:>6}  {mark}")
    print(f"\n{report['passed']} of {report['total']} passed ({report['pass_rate']:.0%})")
    print("By category:", ", ".join(f"{k} {v}" for k, v in report["by_category"].items()))
    print(f"Saved {RESULTS_FILE.relative_to(ROOT)}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
