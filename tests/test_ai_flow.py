"""What lab-ai-flow and the ai_assist Lambda must agree on.

The first group is the generic structure check (every target exists, nothing
dead-ends, every Lambda invoke has an error branch). The rest cover the parts
that are specific to the AI path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FLOWS = Path(__file__).resolve().parent.parent / "terraform" / "flows"
TERRAFORM = Path(__file__).resolve().parent.parent / "terraform"
ROOT = Path(__file__).resolve().parent.parent


def _flow() -> dict:
    text = (FLOWS / "ai.json.tftpl").read_text()
    text = text.replace("${lambda_arn}", "arn:aws:lambda:ap-southeast-2:000000000000:function:x")
    text = text.replace("${queue_arn}", "arn:aws:connect:ap-southeast-2:000000000000:queue/x")
    return json.loads(text)


def _by_id() -> dict:
    return {a["Identifier"]: a for a in _flow()["Actions"]}


def test_the_flow_sends_the_parameter_the_lambda_reads():
    invoke = _by_id()["A08-InvokeAI"]
    assert invoke["Parameters"]["LambdaInvocationAttributes"] == {
        "question": "$.Attributes.question"
    }


def test_the_flow_reads_every_field_the_lambda_returns():
    attrs = _by_id()["A09-SetAiAttributes"]["Parameters"]["Attributes"]
    assert attrs == {
        "aiStatus": "$.External.status",
        "aiAnswer": "$.External.answer",
        "aiReason": "$.External.reason",
        "aiMode": "$.External.mode",
    }


def test_only_answered_and_blocked_are_spoken_everything_else_goes_to_an_agent():
    check = _by_id()["A11-CheckStatus"]
    spoken = {c["Condition"]["Operands"][0] for c in check["Transitions"]["Conditions"]}
    assert spoken == {"answered", "blocked"}
    assert check["Transitions"]["NextAction"] == "A13-SayFallback"
    assert any(
        e["ErrorType"] == "NoMatchingCondition" and e["NextAction"] == "A13-SayFallback"
        for e in check["Transitions"]["Errors"]
    )


def test_a_crash_or_timeout_of_the_lambda_lands_on_the_fallback():
    invoke = _by_id()["A08-InvokeAI"]
    assert invoke["Transitions"]["Errors"][0]["NextAction"] == "A10-SetLambdaError"
    assert _by_id()["A10-SetLambdaError"]["Transitions"]["NextAction"] == "A13-SayFallback"


def test_every_menu_digit_sets_a_question_and_an_id():
    menu = _by_id()["A03-Menu"]
    digits = {
        c["Condition"]["Operands"][0]: c["NextAction"] for c in menu["Transitions"]["Conditions"]
    }
    assert set(digits) == {"1", "2", "3", "4"}
    ids = set()
    for target in digits.values():
        attrs = _by_id()[target]["Parameters"]["Attributes"]
        assert attrs["question"].strip()
        ids.add(attrs["questionId"])
    assert ids == {"hours", "stock_tip", "prompt_attack", "callback"}


def test_no_input_goes_to_an_agent_not_to_the_ai():
    menu = _by_id()["A03-Menu"]
    assert menu["Transitions"]["NextAction"] == "A13-SayFallback"
    for error in menu["Transitions"]["Errors"]:
        assert error["NextAction"] == "A13-SayFallback"


def test_both_spoken_paths_end_in_the_queue_transfer():
    flow = _by_id()
    for name in ("A12-SayAnswer", "A13-SayFallback"):
        assert flow[name]["Transitions"]["NextAction"] == "A14-SetWorkingQueue"


def test_the_flow_time_limit_is_inside_the_connect_ceiling_and_above_the_lambda_timeout():
    limit = int(_by_id()["A08-InvokeAI"]["Parameters"]["InvocationTimeLimitSeconds"])
    lambda_timeout = int(
        re.search(r"timeout\s*=\s*(\d+)", (TERRAFORM / "ai_assist.tf").read_text()).group(1)
    )
    assert lambda_timeout < limit <= 8


def test_terraform_gives_the_lambda_the_switch_and_only_that_parameter():
    assistant = (TERRAFORM / "ai_assist.tf").read_text()
    assert "AI_MODE_PARAM" in assistant
    flow_tf = (TERRAFORM / "ai_flow.tf").read_text()
    assert "ssm:GetParameter" in flow_tf
    assert "ignore_changes = [value]" in flow_tf
    assert 'value       = "off"' in flow_tf  # starts switched off


def test_the_test_modes_are_documented_where_the_flow_is_built():
    guide = (ROOT / "docs" / "AI-FLOW-LAB.md").read_text()
    for mode in ("off", "shadow", "live", "test_fail", "test_crash", "test_slow"):
        assert mode in guide


TERMINAL = {"EndFlowExecution", "DisconnectParticipant"}


def _targets(action: dict) -> list[str]:
    transitions = action.get("Transitions", {})
    out = []
    if transitions.get("NextAction"):
        out.append(transitions["NextAction"])
    out += [e["NextAction"] for e in transitions.get("Errors", [])]
    out += [c["NextAction"] for c in transitions.get("Conditions", [])]
    return out


def test_structure_every_transition_points_at_a_real_action():
    flow = _flow()
    ids = {a["Identifier"] for a in flow["Actions"]}
    assert flow["StartAction"] in ids
    for action in flow["Actions"]:
        for target in _targets(action):
            assert target in ids, f"{action['Identifier']} -> {target}"


def test_structure_no_branch_drops_the_caller_in_silence():
    for action in _flow()["Actions"]:
        if action["Type"] in TERMINAL:
            continue
        assert action["Transitions"].get("NextAction"), action["Identifier"]


def test_structure_every_action_is_reachable_and_listed_in_metadata():
    flow = _flow()
    ids = _by_id()
    seen, stack = set(), [flow["StartAction"]]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack += _targets(ids[current])
    assert seen == set(ids)
    assert set(flow["Metadata"]["ActionMetadata"]) == set(ids)


def test_structure_queue_transfer_handles_a_full_queue():
    transfer = next(a for a in _flow()["Actions"] if a["Type"] == "TransferContactToQueue")
    errors = {e["ErrorType"] for e in transfer["Transitions"]["Errors"]}
    assert "QueueAtCapacity" in errors
