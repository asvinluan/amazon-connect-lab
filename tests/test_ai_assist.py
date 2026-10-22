"""Unit tests for the AI assist Lambda and its evaluation set.

Bedrock is a MagicMock, so nothing here touches AWS. The tests pin the
properties that make a guarded model call safe to put behind a phone line:

  1. The guardrail is attached to every call, by id and numbered version, and
     the caller's text is marked as guard content.
  2. A guardrail intervention is a normal outcome ("blocked"), not an error.
  3. Every failure returns a safe sentence and never raises.
  4. The question is never written to the logs.

Run:  pytest -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "lambda" / "ai_assist" / "handler.py"
EVAL_SCRIPT = ROOT / "scripts" / "run_ai_eval.py"
CASES = ROOT / "evals" / "guardrail_cases.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ai(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "apac.test-model-v1:0")
    monkeypatch.setenv("GUARDRAIL_ID", "gr-test123")
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")
    module = _load("ai_assist_handler", HANDLER)
    fake = MagicMock()
    module._bedrock = fake
    return module, fake


def _answered(text: str = "We are open 8am to 6pm, Monday to Friday.") -> dict:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    }


def _blocked(*policies: str) -> dict:
    assessment = {name: {"detail": "x"} for name in policies}
    return {
        "stopReason": "guardrail_intervened",
        "output": {"message": {"role": "assistant", "content": [{"text": "blocked text"}]}},
        "trace": {"guardrail": {"inputAssessment": {"gr-test123": assessment}}},
    }


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Converse")


# ----------------------------------------------------------------- happy path


def test_answers_a_question(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    out = module.lambda_handler({"question": "What are your hours?"}, None)
    assert out["status"] == "answered"
    assert "8am" in out["answer"]
    assert out["reason"] == ""


def test_every_value_is_a_string_for_connect(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    out = module.lambda_handler({"question": "Hours?"}, None)
    assert set(out) == {"status", "answer", "reason"}
    assert all(isinstance(v, str) for v in out.values())


def test_reads_the_question_from_a_connect_event(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    event = {"Details": {"Parameters": {"question": "Hours?"}}}
    assert module.lambda_handler(event, None)["status"] == "answered"


# ---------------------------------------------------------------- the guardrail


def test_guardrail_is_attached_by_id_and_numbered_version(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    module.lambda_handler({"question": "Hours?"}, None)
    cfg = fake.converse.call_args.kwargs["guardrailConfig"]
    assert cfg["guardrailIdentifier"] == "gr-test123"
    assert cfg["guardrailVersion"] == "3"
    assert cfg["guardrailVersion"] != "DRAFT"
    assert cfg["trace"] == "enabled"


def test_question_is_sent_as_guard_content(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    module.lambda_handler({"question": "  Hours?  "}, None)
    message = fake.converse.call_args.kwargs["messages"][0]
    assert message["content"] == [{"guardContent": {"text": {"text": "Hours?"}}}]


def test_system_prompt_is_grounded_and_has_limits(ai):
    module, fake = ai
    fake.converse.return_value = _answered()
    module.lambda_handler({"question": "Hours?"}, None)
    kwargs = fake.converse.call_args.kwargs
    assert kwargs["system"] == [{"text": module.SYSTEM_PROMPT}]
    assert "Never guess" in module.SYSTEM_PROMPT
    assert "financial, legal or medical advice" in module.SYSTEM_PROMPT
    assert kwargs["inferenceConfig"]["maxTokens"] == module.MAX_OUTPUT_TOKENS
    assert kwargs["inferenceConfig"]["temperature"] <= 0.3


def test_intervention_is_blocked_not_error(ai):
    module, fake = ai
    fake.converse.return_value = _blocked("topicPolicy")
    out = module.lambda_handler({"question": "Which stock should I buy?"}, None)
    assert out["status"] == "blocked"
    assert out["answer"] == module.BLOCKED_ANSWER
    assert out["reason"] == "guardrail:topicPolicy"


def test_reports_every_policy_that_fired_sorted(ai):
    module, fake = ai
    fake.converse.return_value = _blocked("contentPolicy", "topicPolicy")
    out = module.lambda_handler({"question": "x"}, None)
    assert out["reason"] == "guardrail:contentPolicy,topicPolicy"


def test_model_text_is_not_returned_when_blocked(ai):
    module, fake = ai
    fake.converse.return_value = _blocked("contentPolicy")
    out = module.lambda_handler({"question": "x"}, None)
    assert "blocked text" not in out["answer"]


def test_output_side_intervention_is_found(ai):
    module, fake = ai
    response = {
        "stopReason": "guardrail_intervened",
        "output": {"message": {"content": [{"text": "x"}]}},
        "trace": {
            "guardrail": {
                "outputAssessments": {"gr-test123": [{"sensitiveInformationPolicy": {"a": 1}}]}
            }
        },
    }
    fake.converse.return_value = response
    out = module.lambda_handler({"question": "x"}, None)
    assert out["reason"] == "guardrail:sensitiveInformationPolicy"


# ---------------------------------------------------------------- never raises


@pytest.mark.parametrize("bad", [None, "", "   ", 42, ["a"]])
def test_empty_or_non_string_question_is_an_error_without_calling_the_model(ai, bad):
    module, fake = ai
    out = module.lambda_handler({"question": bad}, None)
    assert out["status"] == "error"
    assert out["reason"] == "empty_question"
    fake.converse.assert_not_called()


def test_overlong_question_is_rejected_without_calling_the_model(ai):
    module, fake = ai
    out = module.lambda_handler({"question": "a" * (module.MAX_QUESTION_CHARS + 1)}, None)
    assert out["reason"] == "question_too_long"
    fake.converse.assert_not_called()


def test_missing_configuration_is_an_error_not_a_crash(ai, monkeypatch):
    module, fake = ai
    monkeypatch.delenv("GUARDRAIL_ID")
    out = module.lambda_handler({"question": "Hours?"}, None)
    assert out["reason"] == "config"
    fake.converse.assert_not_called()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ReadTimeoutError(endpoint_url="https://x"), "timeout"),
        (ConnectTimeoutError(endpoint_url="https://x"), "timeout"),
        (_client_error("ThrottlingException"), "bedrock_ThrottlingException"),
        (_client_error("AccessDeniedException"), "bedrock_AccessDeniedException"),
    ],
)
def test_bedrock_failures_return_a_safe_sentence(ai, exc, reason):
    module, fake = ai
    fake.converse.side_effect = exc
    out = module.lambda_handler({"question": "Hours?"}, None)
    assert out["status"] == "error"
    assert out["reason"] == reason
    assert out["answer"] == module.ERROR_ANSWER


def test_empty_model_reply_is_an_error(ai):
    module, fake = ai
    fake.converse.return_value = {"stopReason": "end_turn", "output": {"message": {"content": []}}}
    out = module.lambda_handler({"question": "Hours?"}, None)
    assert out["reason"] == "bad_response"


def test_long_answer_is_truncated(ai):
    module, fake = ai
    fake.converse.return_value = _answered("word " * 500)
    out = module.lambda_handler({"question": "Hours?"}, None)
    assert len(out["answer"]) <= module.MAX_ANSWER_CHARS


# ---------------------------------------------------------------- observability


def test_log_line_is_json_for_metric_filters_and_omits_the_question(ai, capsys):
    module, fake = ai
    fake.converse.return_value = _blocked("topicPolicy")
    secret_question = "my card is 4111 1111 1111 1111 should I buy shares"
    module.lambda_handler({"question": secret_question}, None)
    line = capsys.readouterr().out.strip()
    data = json.loads(line)
    assert data["event"] == "ai_assist"
    assert data["status"] == "blocked"
    assert data["reason"] == "guardrail:topicPolicy"
    assert data["stop_reason"] == "guardrail_intervened"
    assert isinstance(data["latency_ms"], int)
    assert data["question_chars"] == len(secret_question)
    assert "4111" not in line
    assert "shares" not in line


def test_errors_are_logged_too(ai, capsys):
    module, fake = ai
    fake.converse.side_effect = _client_error("ThrottlingException")
    module.lambda_handler({"question": "Hours?"}, None)
    data = json.loads(capsys.readouterr().out.strip())
    assert data["status"] == "error"
    assert data["reason"] == "bedrock_ThrottlingException"


# ------------------------------------------------------------- the evaluation set


@pytest.fixture(scope="module")
def runner():
    return _load("run_ai_eval", EVAL_SCRIPT)


def test_case_file_is_well_formed(runner):
    cases = runner.load_cases(CASES)
    assert len(cases) == 15
    assert len({c["id"] for c in cases}) == 15
    assert {c["expect"] for c in cases} == {"answered", "blocked"}
    assert sum(c["expect"] == "blocked" for c in cases) == 8
    for case in cases:
        assert case["prompt"].strip()
        assert case["category"]


def test_case_set_covers_every_guardrail_control(runner):
    categories = {c["category"] for c in runner.load_cases(CASES)}
    assert {
        "denied_topic",
        "prompt_attack",
        "pii",
        "content_filter",
        "grounding",
        "in_scope",
    } <= categories


def test_no_real_personal_data_in_the_cases(runner):
    text = CASES.read_text(encoding="utf-8")
    assert "4111 1111 1111 1111" in text  # the public test card number, on purpose
    assert "@" not in text


def test_judge_passes_a_matching_status(runner):
    case = {"expect": "blocked"}
    assert runner.judge(case, {"status": "blocked"})[0] is True


def test_judge_fails_a_wrong_status_and_says_why(runner):
    ok, note = runner.judge({"expect": "blocked"}, {"status": "answered", "reason": ""})
    assert ok is False
    assert "expected blocked, got answered" in note


def test_judge_checks_wording_on_grounding_cases(runner):
    case = {"expect": "answered", "contains_any": ["don't have", "agent"]}
    assert runner.judge(case, {"status": "answered", "answer": "I'll get you an Agent."})[0] is True
    assert runner.judge(case, {"status": "answered", "answer": "The CEO is Sam."})[0] is False


def test_evaluate_reports_pass_rate_and_categories(runner):
    cases = [
        {"id": "a", "category": "x", "expect": "blocked", "prompt": "p1"},
        {"id": "b", "category": "x", "expect": "answered", "prompt": "p2"},
        {"id": "c", "category": "y", "expect": "blocked", "prompt": "p3"},
        {"id": "d", "category": "y", "expect": "blocked", "prompt": "p4"},
    ]
    replies = {
        "p1": {"status": "blocked"},
        "p2": {"status": "blocked"},  # wrong
        "p3": {"status": "blocked"},
        "p4": {"status": "blocked"},
    }
    report = runner.evaluate(cases, lambda q: replies[q])
    assert report["total"] == 4
    assert report["passed"] == 3
    assert report["pass_rate"] == 0.75
    assert report["by_category"] == {"x": "1/2", "y": "2/2"}
    assert [c["passed"] for c in report["cases"]] == [True, False, True, True]
