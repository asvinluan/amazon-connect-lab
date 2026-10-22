"""Unit tests for the Salesforce caller lookup.

Nothing here touches AWS or Salesforce. Secrets Manager is a MagicMock and the
single network function in the handler (``_open``) is replaced with a fake that
answers like Salesforce does. The credentials are obviously fake.

What these tests pin down, because each is easy to break by accident:

  1. A "+" in the phone number is sent as %2B. A raw "+" in a URL query string
     means "space", and the lookup would quietly find nobody.
  2. Every failure (bad credential, timeout, outage, bad secret) returns a
     usable answer and never raises, so the caller still gets through.
  3. The client secret is never sent to a host that is not Salesforce, and
     neither the secret nor the access token ever reaches a log line or the
     response.
  4. Every value returned is a string, as Connect requires.

Run:  pytest -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

HANDLER = Path(__file__).resolve().parents[1] / "lambda" / "salesforce_lookup" / "handler.py"
INSTANCE = "https://example-dev-ed.develop.my.salesforce.com"
SECRET = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret-value",
    "instance_url": INSTANCE,
}
TOKEN = "test-access-token-value"
CONTACT = {"Id": "003000000000001", "Name": "Test Caller One", "Account": {"Name": "Example Pty"}}


class _Resp:
    """Stands in for the object urlopen returns."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(code: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps(payload or {}).encode())
    return urllib.error.HTTPError(INSTANCE, code, "error", {}, body)  # type: ignore[arg-type]


class _Salesforce:
    """A fake Salesforce: routes token and query requests, records every call."""

    def __init__(self) -> None:
        self.token_replies: list = [_Resp({"access_token": TOKEN})]
        self.query_replies: list = [_Resp({"records": [CONTACT]})]
        self.requests: list = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        queue = self.token_replies if req.full_url.endswith("/oauth2/token") else self.query_replies
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def tokens(self) -> list:
        return [r for r in self.requests if r.full_url.endswith("/oauth2/token")]

    @property
    def queries(self) -> list:
        return [r for r in self.requests if "/query?" in r.full_url]


@pytest.fixture()
def lookup(monkeypatch):
    """A freshly imported handler (so its caches start empty) wired to fakes."""
    monkeypatch.setenv("SALESFORCE_SECRET_ID", "test/salesforce")
    spec = importlib.util.spec_from_file_location("salesforce_lookup_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": json.dumps(SECRET)}
    module._secrets = secrets

    sf = _Salesforce()
    module._open = sf
    return module, secrets, sf


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


def test_known_caller_returns_name_and_company(lookup):
    module, _, _ = lookup

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "found"
    assert result["callerName"] == "Test Caller One"
    assert result["company"] == "Example Pty"


def test_contact_without_an_account_returns_blank_company(lookup):
    module, _, sf = lookup
    sf.query_replies = [_Resp({"records": [{"Name": "Solo Caller", "Account": None}]})]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["callerName"] == "Solo Caller"
    assert result["company"] == ""


def test_unknown_caller_is_not_an_error(lookup):
    """No matching Contact is a successful lookup with a negative answer."""
    module, _, sf = lookup
    sf.query_replies = [_Resp({"records": []})]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "not_found"
    assert result["callerName"] == "there"


def test_plus_is_sent_as_percent_2b(lookup):
    module, _, sf = lookup

    module.lambda_handler(_event("+61455500001"), None)

    url = sf.queries[0].full_url
    query_string = url.split("?", 1)[1]
    assert "%2B61455500001" in query_string
    assert "+" not in query_string  # a raw plus would be read as a space
    soql = urllib.parse.parse_qs(query_string)["q"][0]
    assert "FROM Contact" in soql
    assert "'+61455500001'" in soql


def test_local_number_is_normalised_and_both_formats_are_queried(lookup):
    """0455 500 001 and +61455500001 are the same person."""
    module, _, sf = lookup

    module.lambda_handler(_event("0455500001"), None)

    soql = urllib.parse.parse_qs(sf.queries[0].full_url.split("?", 1)[1])["q"][0]
    assert "'+61455500001'" in soql
    assert "'0455500001'" in soql


def test_token_request_and_query_are_shaped_correctly(lookup):
    module, _, sf = lookup

    module.lambda_handler(_event(), None)

    token_req = sf.tokens[0]
    assert token_req.full_url == f"{INSTANCE}/services/oauth2/token"
    form = urllib.parse.parse_qs(token_req.data.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [SECRET["client_id"]]
    assert sf.queries[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert sf.queries[0].full_url.startswith(f"{INSTANCE}/services/data/v")


def test_token_is_cached_between_calls(lookup):
    module, secrets, sf = lookup

    module.lambda_handler(_event(), None)
    module.lambda_handler(_event(), None)

    assert len(sf.tokens) == 1
    assert len(sf.queries) == 2
    assert secrets.get_secret_value.call_count == 1


def test_expired_token_is_refreshed_once(lookup):
    module, _, sf = lookup
    sf.query_replies = [_http_error(401), _Resp({"records": [CONTACT]})]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "found"
    assert len(sf.tokens) == 2


def test_repeated_401_gives_up_instead_of_looping(lookup):
    module, _, sf = lookup
    sf.query_replies = [_http_error(401)]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
    assert result["reason"] == "query_http_401"
    assert len(sf.tokens) == 2  # one refresh, then stop


def test_rejected_credentials_do_not_raise(lookup):
    module, _, sf = lookup
    sf.token_replies = [_http_error(400, {"error": "invalid_client"})]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
    assert result["reason"] == "token_http_400"
    assert result["callerName"] == "there"  # the caller still gets a greeting
    assert sf.queries == []


@pytest.mark.parametrize("stage", ["token", "query"])
def test_timeout_falls_back_instead_of_hanging(lookup, stage):
    """A slow Salesforce must not hold the caller until Connect's 8-second cut-off."""
    module, _, sf = lookup
    slow = urllib.error.URLError(TimeoutError("timed out"))
    if stage == "token":
        sf.token_replies = [slow]
    else:
        sf.query_replies = [slow]

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "degraded"
    assert result["reason"] == f"{stage}_timeout"
    assert result["callerName"] == "there"


def test_exhausted_budget_makes_no_calls(lookup):
    module, _, sf = lookup
    module.TOTAL_BUDGET = 0

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "degraded"
    assert result["reason"] == "budget_exhausted"
    assert sf.requests == []


def test_invalid_json_secret_is_reported_clearly(lookup):
    """The classic mistake: instance_url pasted without its quotes."""
    module, secrets, sf = lookup
    secrets.get_secret_value.return_value = {
        "SecretString": '{"client_id": "a", "client_secret": "b", "instance_url": https://x}'
    }

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
    assert result["reason"] == "secret_invalid_json"
    assert sf.requests == []


def test_secret_missing_a_key_is_reported(lookup):
    module, secrets, _ = lookup
    secrets.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "a"})}

    result = module.lambda_handler(_event(), None)

    assert result["reason"] == "secret_missing_keys"


def test_client_secret_is_never_sent_to_a_non_salesforce_host(lookup):
    module, secrets, sf = lookup
    bad = {**SECRET, "instance_url": "https://login.attacker.example.com"}
    secrets.get_secret_value.return_value = {"SecretString": json.dumps(bad)}

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["reason"] == "instance_url_not_salesforce"
    assert sf.requests == []


def test_secrets_manager_failure_does_not_raise(lookup):
    module, secrets, sf = lookup
    secrets.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetSecretValue"
    )

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["status"] == "error"
    assert result["reason"] == "secret_unreachable"
    assert sf.requests == []


def test_withheld_caller_id_is_not_an_error(lookup):
    module, secrets, sf = lookup

    result = module.lambda_handler(_event(None), None)

    _assert_connect_safe(result)
    assert result["status"] == "not_found"
    assert result["reason"] == "no_caller_id"
    assert sf.requests == []
    secrets.get_secret_value.assert_not_called()


def test_missing_secret_env_does_not_raise(lookup, monkeypatch):
    module, _, _ = lookup
    monkeypatch.setattr(module, "SECRET_ID", "")

    result = module.lambda_handler(_event(), None)

    _assert_connect_safe(result)
    assert result["reason"] == "secret_env_missing"


def test_secret_and_token_never_reach_the_logs_or_the_response(lookup, caplog):
    module, _, sf = lookup
    caplog.set_level(logging.DEBUG)

    ok = module.lambda_handler(_event(), None)
    sf.token_replies = [_http_error(400, {"error": "invalid_client"})]
    module._cache.update({"token": None, "token_at": 0.0})
    failed = module.lambda_handler(_event(), None)

    for blob in (caplog.text, json.dumps(ok), json.dumps(failed)):
        assert SECRET["client_secret"] not in blob
        assert TOKEN not in blob
