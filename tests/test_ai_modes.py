"""Tests for the AI mode switch (off, shadow, live) and the break-it test modes.

Nothing here touches AWS: Bedrock and Parameter Store are MagicMocks. What the
tests pin down is the promise the call flow relies on:

  1. off never calls the model.
  2. shadow runs the model but never lets the caller hear it.
  3. If the switch cannot be read the function fails SAFE to off.
  4. The switch is cached, so a busy line does not hammer Parameter Store.
  5. Every log line carries the mode and the contact id, never the question.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "lambda" / "ai_assist" / "handler.py"
PARAM = "/test-lab/ai/mode"
QUESTION = "What are your opening hours?"


def _load():
    spec = importlib.util.spec_from_file_location("ai_modes_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(question: str = QUESTION, contact_id: str = "c-123") -> dict:
    return {
        "Details": {
            "ContactData": {"ContactId": contact_id},
            "Parameters": {"question": question},
        }
    }


def _answered(text: str = "We are open 8am to 6pm, Monday to Friday.") -> dict:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    }


def _blocked() -> dict:
    return {
        "stopReason": "guardrail_intervened",
        "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
        "trace": {"guardrail": {"inputAssessment": {"gr": {"topicPolicy": {"d": 1}}}}},
    }


@pytest.fixture()
def lab(monkeypatch):
    """A handler wired to a fake Parameter Store and a fake Bedrock."""
    monkeypatch.setenv("MODEL_ID", "apac.test-model-v1:0")
    monkeypatch.setenv("GUARDRAIL_ID", "gr-test123")
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")
    monkeypatch.setenv("AI_MODE_PARAM", PARAM)
    module = _load()
    bedrock, ssm = MagicMock(), MagicMock()
    bedrock.converse.return_value = _answered()
    module._bedrock = bedrock
    module._ssm = ssm
    monkeypatch.setattr(module.time, "sleep", MagicMock())

    def set_mode(value: str) -> None:
        ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
        module._mode_cache.update(value=module.FAIL_SAFE_MODE, at=None)

    return module, bedrock, ssm, set_mode


# ------------------------------------------------------------------------ off


def test_off_never_calls_the_model(lab):
    module, bedrock, _, set_mode = lab
    set_mode("off")
    out = module.lambda_handler(_event(), None)
    assert out == {"status": "disabled", "answer": "", "reason": "mode_off", "mode": "off"}
    bedrock.converse.assert_not_called()


# --------------------------------------------------------------------- shadow


def test_shadow_runs_the_model_but_the_caller_hears_nothing(lab):
    module, bedrock, _, set_mode = lab
    set_mode("shadow")
    out = module.lambda_handler(_event(), None)
    assert bedrock.converse.call_count == 1
    assert out["status"] == "shadow"
    assert out["answer"] == ""
    assert out["reason"] == "would_be_answered"


def test_shadow_records_what_a_block_would_have_been(lab):
    module, bedrock, _, set_mode = lab
    set_mode("shadow")
    bedrock.converse.return_value = _blocked()
    out = module.lambda_handler(_event("Which stock should I buy?"), None)
    assert (out["status"], out["answer"], out["reason"]) == ("shadow", "", "would_be_blocked")


def test_shadow_log_keeps_the_real_outcome_for_later_comparison(lab, capsys):
    module, _, _, set_mode = lab
    set_mode("shadow")
    module.lambda_handler(_event(), None)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["status"] == "answered"
    assert line["mode"] == "shadow"
    assert line["contact_id"] == "c-123"


# ----------------------------------------------------------------------- live


def test_live_returns_the_answer_to_the_caller(lab):
    module, _, _, set_mode = lab
    set_mode("live")
    out = module.lambda_handler(_event(), None)
    assert out["status"] == "answered"
    assert out["answer"].startswith("We are open")
    assert out["mode"] == "live"


def test_without_a_parameter_name_the_function_behaves_as_live(lab, monkeypatch):
    """Console tests and the evaluation script do not set the switch."""
    module, bedrock, ssm, _ = lab
    monkeypatch.delenv("AI_MODE_PARAM")
    out = module.lambda_handler(_event(), None)
    assert out["status"] == "answered"
    ssm.get_parameter.assert_not_called()


# ------------------------------------------------------------ fail safe, cache


def test_an_unreadable_switch_fails_safe_to_off(lab):
    module, bedrock, ssm, set_mode = lab
    set_mode("live")
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter"
    )
    out = module.lambda_handler(_event(), None)
    assert out["status"] == "disabled"
    bedrock.converse.assert_not_called()


@pytest.mark.parametrize("bad", ["", "LIVE NOW", "yes", "true"])
def test_an_unknown_value_fails_safe_to_off(lab, bad):
    module, bedrock, _, set_mode = lab
    set_mode(bad)
    out = module.lambda_handler(_event(), None)
    assert out["status"] == "disabled"
    bedrock.converse.assert_not_called()


def test_the_value_is_trimmed_and_case_insensitive(lab):
    module, _, _, set_mode = lab
    set_mode("  Live \n")
    assert module.lambda_handler(_event(), None)["status"] == "answered"


def test_the_switch_is_cached_between_calls(lab):
    module, _, ssm, set_mode = lab
    set_mode("live")
    for _ in range(5):
        module.lambda_handler(_event(), None)
    assert ssm.get_parameter.call_count == 1


def test_the_cache_expires(lab, monkeypatch):
    module, _, ssm, set_mode = lab
    set_mode("live")
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    module.lambda_handler(_event(), None)
    clock[0] += module.MODE_CACHE_SECONDS + 1
    module.lambda_handler(_event(), None)
    assert ssm.get_parameter.call_count == 2


# ---------------------------------------------------------- break-it test modes


def test_test_fail_returns_an_error_the_flow_can_handle(lab):
    module, bedrock, _, set_mode = lab
    set_mode("test_fail")
    out = module.lambda_handler(_event(), None)
    assert (out["status"], out["reason"]) == ("error", "test_fail")
    assert "agent" in out["answer"]
    bedrock.converse.assert_not_called()


def test_test_crash_raises_so_the_flows_error_branch_is_exercised(lab):
    module, _, _, set_mode = lab
    set_mode("test_crash")
    with pytest.raises(RuntimeError):
        module.lambda_handler(_event(), None)


def test_test_slow_outlasts_the_lambda_timeout(lab):
    module, _, _, set_mode = lab
    set_mode("test_slow")
    module.lambda_handler(_event(), None)
    module.time.sleep.assert_called_once_with(module.TEST_SLOW_SECONDS)
    assert module.TEST_SLOW_SECONDS > 7  # the Lambda timeout in terraform/ai_assist.tf


# ---------------------------------------------------------------- logging rules


def test_the_question_is_never_logged(lab, capsys):
    module, _, _, set_mode = lab
    set_mode("live")
    module.lambda_handler(_event("Which of my secret things is private?"), None)
    assert "secret" not in capsys.readouterr().out


def test_a_missing_contact_id_does_not_break_the_log(lab, capsys):
    module, _, _, set_mode = lab
    set_mode("live")
    module.lambda_handler({"question": QUESTION}, None)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["contact_id"] == ""
