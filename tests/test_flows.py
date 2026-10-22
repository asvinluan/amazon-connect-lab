"""Structural checks on the contact flow templates.

A flow is JSON that Amazon Connect only validates when Terraform applies it, so
a typo in a transition target shows up as a failed apply or, worse, a call that
drops in silence. These tests catch that in CI, with no AWS access.
"""

import json
from pathlib import Path

import pytest

FLOWS = Path(__file__).resolve().parent.parent / "terraform" / "flows"
TERMINAL = {"EndFlowExecution", "DisconnectParticipant"}


def _load(name: str) -> dict:
    text = (FLOWS / name).read_text()
    text = text.replace("${lambda_arn}", "arn:aws:lambda:ap-southeast-2:000000000000:function:x")
    text = text.replace("${queue_arn}", "arn:aws:connect:ap-southeast-2:000000000000:queue/x")
    return json.loads(text)


def _targets(action: dict) -> list[str]:
    transitions = action.get("Transitions", {})
    out = []
    if transitions.get("NextAction"):
        out.append(transitions["NextAction"])
    out += [e["NextAction"] for e in transitions.get("Errors", [])]
    out += [c["NextAction"] for c in transitions.get("Conditions", [])]
    return out


@pytest.fixture(params=["main.json.tftpl", "salesforce.json.tftpl"])
def flow(request):
    return _load(request.param)


def test_templates_are_valid_json_once_rendered(flow):
    assert flow["Version"] == "2019-10-30"
    assert flow["Actions"]


def test_start_action_exists(flow):
    ids = {a["Identifier"] for a in flow["Actions"]}
    assert flow["StartAction"] in ids


def test_every_transition_points_at_a_real_action(flow):
    ids = {a["Identifier"] for a in flow["Actions"]}
    for action in flow["Actions"]:
        for target in _targets(action):
            assert target in ids, f"{action['Identifier']} -> {target}"


def test_every_branch_terminates(flow):
    """A non-terminal block with no next action drops the caller in silence."""
    for action in flow["Actions"]:
        if action["Type"] in TERMINAL:
            continue
        assert action["Transitions"].get("NextAction"), action["Identifier"]


def test_every_action_is_reachable(flow):
    ids = {a["Identifier"]: a for a in flow["Actions"]}
    seen, stack = set(), [flow["StartAction"]]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack += _targets(ids[current])
    assert seen == set(ids)


def test_lambda_invoke_has_an_error_branch_and_a_short_limit(flow):
    """Connect allows 8 seconds. An unhandled Lambda failure must not end the call."""
    invokes = [a for a in flow["Actions"] if a["Type"] == "InvokeLambdaFunction"]
    assert invokes
    for action in invokes:
        assert action["Transitions"]["Errors"], action["Identifier"]
        assert int(action["Parameters"]["InvocationTimeLimitSeconds"]) <= 8


def test_queue_transfer_handles_a_full_queue(flow):
    transfer = next(a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue")
    errors = {e["ErrorType"] for e in transfer["Transitions"]["Errors"]}
    assert "QueueAtCapacity" in errors


def test_metadata_lists_every_action(flow):
    ids = {a["Identifier"] for a in flow["Actions"]}
    assert set(flow["Metadata"]["ActionMetadata"]) == ids


def test_salesforce_flow_reads_the_attributes_the_lambda_returns():
    flow = _load("salesforce.json.tftpl")
    attrs = next(a for a in flow["Actions"] if a["Type"] == "UpdateContactAttributes")
    values = attrs["Parameters"]["Attributes"]
    assert values["callerName"] == "$.External.callerName"
    assert values["lookupStatus"] == "$.External.status"


def test_fallback_greeting_has_a_name_to_speak():
    """If the lookup fails the flow still greets someone, never 'Hello .'."""
    flow = _load("salesforce.json.tftpl")
    fallback = next(a for a in flow["Actions"] if a["Identifier"] == "A05-SetUnknown")
    assert fallback["Parameters"]["Attributes"]["callerName"] == "there"
