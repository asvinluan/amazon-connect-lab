"""AI assist: a guarded generative answer for a contact flow.

A caller's question goes to a foundation model through the Amazon Bedrock
Converse API, with a Bedrock guardrail attached. The function returns flat
strings (``status``, ``answer``, ``reason``) so Amazon Connect can read them
as external attributes, the same shape as the Salesforce lookup.

Four properties this lab is built to show
-----------------------------------------
  1. GUARDRAIL FIRST. The guardrail is not an afterthought in the prompt. It is
     attached to every call and pinned to a numbered version, so the behaviour
     under test is the behaviour in production. It denies financial and legal
     advice, filters hate, insults, violence and prompt attacks, and blocks
     card numbers. The caller's question is wrapped in ``guardContent`` so the
     guardrail evaluates exactly that text, including for prompt attacks.

  2. GROUNDED PROMPT. ``system_prompt.txt`` holds the only facts the model may
     use and tells it to say "I do not have that" rather than guess.

  3. NEVER RAISES. Connect has an 8 second ceiling on a Lambda invoke. A slow
     or failing model must not leave the caller in silence, so every failure
     returns ``status = "error"`` and a safe sentence, and the flow carries on
     to an agent.

  4. OBSERVABLE WITHOUT LEAKING. Each call writes one JSON log line with the
     status, which guardrail policies fired, latency and the length of the
     question. The question itself is never logged here. Metric filters in
     Terraform turn those lines into CloudWatch metrics and alarms.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 600
MAX_OUTPUT_TOKENS = 200

BLOCKED_ANSWER = "Sorry, I can't help with that here. I can connect you to an agent."
ERROR_ANSWER = "Sorry, I can't answer that right now. I'll connect you to an agent."

SYSTEM_PROMPT = (Path(__file__).with_name("system_prompt.txt")).read_text(encoding="utf-8").strip()

POLICIES = (
    "topicPolicy",
    "contentPolicy",
    "sensitiveInformationPolicy",
    "wordPolicy",
    "contextualGroundingPolicy",
)

# One attempt, short timeouts: a retry would spend the 8 second Connect budget.
_bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}),
)


def _result(status: str, answer: str, reason: str = "") -> dict[str, str]:
    return {"status": status, "answer": answer[:MAX_ANSWER_CHARS], "reason": reason}


def _question(event: dict[str, Any]) -> Any:
    """Connect sends Details.Parameters; a console test can send the key directly."""
    params = (event.get("Details") or {}).get("Parameters") or {}
    return params.get("question", event.get("question"))


def _triggered_policies(response: dict[str, Any]) -> list[str]:
    """Which guardrail policies fired, from the trace. Names only, never content."""
    guard = (response.get("trace") or {}).get("guardrail") or {}
    assessments: list[Any] = list((guard.get("inputAssessment") or {}).values())
    for items in (guard.get("outputAssessments") or {}).values():
        assessments.extend(items)
    fired: set[str] = set()
    for item in assessments:
        if isinstance(item, dict):
            fired.update(name for name in POLICIES if item.get(name))
    return sorted(fired)


def _answer_text(response: dict[str, Any]) -> str:
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    return " ".join(part["text"] for part in content if isinstance(part, dict) and "text" in part)


def _log(status: str, reason: str, stop_reason: str, started: float, question_chars: int) -> None:
    """One JSON line per call. CloudWatch metric filters read these fields."""
    # A bare JSON line (not the logging module) is what the metric filters match.
    print(
        json.dumps(
            {
                "event": "ai_assist",
                "status": status,
                "reason": reason,
                "stop_reason": stop_reason,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "question_chars": question_chars,
            }
        )
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    started = time.monotonic()
    question = _question(event or {})

    if not isinstance(question, str) or not question.strip():
        _log("error", "empty_question", "", started, 0)
        return _result("error", ERROR_ANSWER, "empty_question")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        _log("error", "question_too_long", "", started, len(question))
        return _result("error", ERROR_ANSWER, "question_too_long")

    model_id = os.environ.get("MODEL_ID")
    guardrail_id = os.environ.get("GUARDRAIL_ID")
    guardrail_version = os.environ.get("GUARDRAIL_VERSION")
    if not (model_id and guardrail_id and guardrail_version):
        _log("error", "config", "", started, len(question))
        return _result("error", ERROR_ANSWER, "config")

    guarded = {"guardContent": {"text": {"text": question}}}
    try:
        response = _bedrock.converse(
            modelId=model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [guarded]}],
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0.2},
            guardrailConfig={
                "guardrailIdentifier": guardrail_id,
                "guardrailVersion": guardrail_version,
                "trace": "enabled",
            },
        )
    except (ReadTimeoutError, ConnectTimeoutError):
        _log("error", "timeout", "", started, len(question))
        return _result("error", ERROR_ANSWER, "timeout")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        _log("error", f"bedrock_{code}", "", started, len(question))
        return _result("error", ERROR_ANSWER, f"bedrock_{code}")
    except BotoCoreError:
        _log("error", "bedrock_unreachable", "", started, len(question))
        return _result("error", ERROR_ANSWER, "bedrock_unreachable")

    stop_reason = str(response.get("stopReason", ""))

    if stop_reason == "guardrail_intervened":
        reason = "guardrail:" + ",".join(_triggered_policies(response))
        _log("blocked", reason, stop_reason, started, len(question))
        return _result("blocked", BLOCKED_ANSWER, reason)

    text = _answer_text(response).strip()
    if not text:
        _log("error", "bad_response", stop_reason, started, len(question))
        return _result("error", ERROR_ANSWER, "bad_response")

    _log("answered", "", stop_reason, started, len(question))
    return _result("answered", text)
