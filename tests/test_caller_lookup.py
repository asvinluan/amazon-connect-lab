"""Unit tests for the Connect caller lookup.

The point of these tests is not coverage. It is to pin the three behaviours
that make the function safe inside a contact flow, each of which is easy to
break with an innocent-looking refactor:

  1. It NEVER raises for an expected condition. A raise sends the flow down
     the Error branch, which is for "something broke", not "nobody matched".
  2. Every value it returns is a string. Connect rejects anything else.
  3. A DynamoDB failure degrades to a usable answer instead of hanging until
     Connect's 8-second ceiling cuts the invocation off.

Run:  pytest -q
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ReadTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda" / "caller_lookup"))


@pytest.fixture()
def handler(monkeypatch):
    """Import the module with a stubbed DynamoDB client."""
    monkeypatch.setenv("CUSTOMER_TABLE", "test-customers")
    monkeypatch.setenv("DDB_TIMEOUT_SEC", "2")

    import handler as module

    importlib.reload(module)

    fake = MagicMock()

    # botocore builds exception classes on the client, so the stub needs them
    # or the handler's `except _ddb.exceptions...` lines fail at import time.
    class _Throttled(Exception):
        pass

    fake.exceptions.ProvisionedThroughputExceededException = _Throttled
    module._ddb = fake
    return module, fake


def _event(number: str | None = "+61455500001") -> dict:
    return {
        "Details": {
            "Parameters": {},
            "ContactData": {
                "ContactId": "test-contact-id",
                "CustomerEndpoint": {"Address": number, "Type": "TELEPHONE_NUMBER"},
            },
        }
    }


def _assert_connect_safe(result):
    """Every Connect response must be a flat map of strings under 32 KB."""
    assert isinstance(result, dict)
    for key, value in result.items():
        assert isinstance(key, str), f"key {key!r} is not a string"
        assert isinstance(value, str), f"value for {key!r} is {type(value)}, not str"
    assert len(json.dumps(result).encode()) < 32_000


def test_known_caller_returns_name(handler):
    module, fake = handler
    fake.get_item.return_value = {
        "Item": {
            "phoneNumber": {"S": "+61455500001"},
            "callerName": {"S": "Test Caller One"},
            "segment": {"S": "priority"},
        }
    }

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "found"
    assert result["callerName"] == "Test Caller One"
    assert result["segment"] == "priority"


def test_unknown_caller_is_not_an_error(handler):
    """A caller we have never seen is a successful lookup with no match.

    If this returned an error the flow would take its Error branch, and every
    first-time caller would be treated as a system failure.
    """
    module, fake = handler
    fake.get_item.return_value = {}  # no Item key

    result = module.lambda_handler(_event("+61455509999"), None)

    _assert_connect_safe(result)
    assert result["status"] == "not_found"
    assert result["callerName"] == "there"


def test_withheld_caller_id_does_not_raise(handler):
    module, _ = handler

    result = module.lambda_handler(_event(None), None)

    _assert_connect_safe(result)
    assert result["status"] == "not_found"


def test_dynamodb_timeout_degrades_instead_of_raising(handler):
    """The behaviour that protects the 8-second ceiling.

    If this propagated, Connect would wait for its own cut-off and the caller
    would sit in silence before landing on the Error branch.
    """
    module, fake = handler
    fake.get_item.side_effect = ReadTimeoutError(endpoint_url="https://dynamodb")

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
    assert result["callerName"] == "there"  # caller still gets a greeting


def test_throttling_degrades(handler):
    module, fake = handler
    fake.get_item.side_effect = fake.exceptions.ProvisionedThroughputExceededException()

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "degraded"


def test_local_number_is_normalised(handler):
    """0455 500 001 and +61455500001 must find the same record."""
    module, fake = handler
    fake.get_item.return_value = {
        "Item": {"callerName": {"S": "Test Caller One"}, "segment": {"S": "standard"}}
    }

    module.lambda_handler(_event("0455500001"), None)

    key = fake.get_item.call_args.kwargs["Key"]
    assert key == {"phoneNumber": {"S": "+61455500001"}}


def test_missing_table_env_does_not_raise(handler, monkeypatch):
    module, _ = handler
    monkeypatch.setattr(module, "TABLE_NAME", "")

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
