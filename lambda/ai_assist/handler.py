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

# ------------------------------------------------------------------- the switch
#
# off, shadow and live are the real modes. The three test_* modes exist only to
# break the function on purpose (see docs/AI-FLOW-LAB.md).
MODES = ("off", "shadow", "live", "test_fail", "test_crash", "test_slow")
FAIL_SAFE_MODE = "off"
MODE_CACHE_SECONDS = 15
TEST_SLOW_SECONDS = 9  # longer than the Lambda timeout, so the call times out

_ssm = boto3.client(
    "ssm",
    config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1}),
)
_mode_cache: dict[str, Any] = {"value": FAIL_SAFE_MODE, "at": None}


def _mode() -> str:
    """Read the mode from Parameter Store, cached briefly, failing safe to off.

    If AI_MODE_PARAM is not set (a console test, the evaluation script) the
    function behaves as it always did: live.
    """
    name = os.environ.get("AI_MODE_PARAM")
    if not name:
        return "live"
    now = time.monotonic()
    at = _mode_cache["at"]
    if at is not None and now - at < MODE_CACHE_SECONDS:
        return str(_mode_cache["value"])
    try:
        value = _ssm.get_parameter(Name=name)["Parameter"]["Value"].strip().lower()
    except (ClientError, BotoCoreError):
        value = FAIL_SAFE_MODE
    if value not in MODES:
        value = FAIL_SAFE_MODE
    _mode_cache.update(value=value, at=now)
    return value


def _result(status: str, answer: str, reason: str = "", mode: str = "live") -> dict[str, str]:
    return {
        "status": status,
        "answer": answer[:MAX_ANSWER_CHARS],
        "reason": reason,
        "mode": mode,
    }


def _contact_id(event: dict[str, Any]) -> str:
    """Connect sends the contact id; it lets shadow logs be joined to call records later."""
    data = (event.get("Details") or {}).get("ContactData") or {}
    return str(data.get("ContactId", ""))[:64]


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


def _log(
    status: str,
    reason: str,
    stop_reason: str,
    started: float,
    question_chars: int,
    mode: str = "live",
    contact_id: str = "",
) -> None:
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
                "mode": mode,
                "contact_id": contact_id,
            }
        )
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    started = time.monotonic()
    event = event or {}
    mode = _mode()
    contact_id = _contact_id(event)
    question = _question(event)

    def done(status: str, answer: str, reason: str, stop_reason: str, chars: int) -> dict[str, str]:
        _log(status, reason, stop_reason, started, chars, mode, contact_id)
        return _shadowed(_result(status, answer, reason, mode), mode)

    # ---- the switch comes first: off means no model call at all
    if mode == "off":
        return done("disabled", "", "mode_off", "", 0)
    if mode == "test_crash":
        raise RuntimeError("test_crash: deliberate failure to prove the flow's error branch")
    if mode == "test_slow":
        time.sleep(TEST_SLOW_SECONDS)  # the Lambda timeout cuts this off
    if mode == "test_fail":
        return done("error", ERROR_ANSWER, "test_fail", "", 0)

    if not isinstance(question, str) or not question.strip():
        return done("error", ERROR_ANSWER, "empty_question", "", 0)
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        return done("error", ERROR_ANSWER, "question_too_long", "", len(question))

    model_id = os.environ.get("MODEL_ID")
    guardrail_id = os.environ.get("GUARDRAIL_ID")
    guardrail_version = os.environ.get("GUARDRAIL_VERSION")
    if not (model_id and guardrail_id and guardrail_version):
        return done("error", ERROR_ANSWER, "config", "", len(question))

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
        return done("error", ERROR_ANSWER, "timeout", "", len(question))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        return done("error", ERROR_ANSWER, f"bedrock_{code}", "", len(question))
    except BotoCoreError:
        return done("error", ERROR_ANSWER, "bedrock_unreachable", "", len(question))

    stop_reason = str(response.get("stopReason", ""))

    if stop_reason == "guardrail_intervened":
        reason = "guardrail:" + ",".join(_triggered_policies(response))
        return done("blocked", BLOCKED_ANSWER, reason, stop_reason, len(question))

    text = _answer_text(response).strip()
    if not text:
        return done("error", ERROR_ANSWER, "bad_response", stop_reason, len(question))

    return done("answered", text, "", stop_reason, len(question))


def _shadowed(result: dict[str, str], mode: str) -> dict[str, str]:
    """In shadow mode the model ran and was logged, but the caller must not hear it.

    The real outcome is kept in ``reason`` (for example ``answered``) so the flow
    and the call record both show what the AI WOULD have done.
    """
    if mode != "shadow":
        return result
    return _result("shadow", "", f"would_be_{result['status']}", mode)
