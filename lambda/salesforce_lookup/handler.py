"""Salesforce caller lookup for an Amazon Connect contact flow.

Same job as ``caller_lookup`` (who is calling?), but the answer comes from a
Salesforce Contact instead of a DynamoDB table. It is a separate function on
purpose: the lab's main contact flow still uses caller_lookup, so the working
voice path is untouched; this function is invoked by its own flow,
lab-salesforce-flow.

What it does, in order
----------------------
  1. Reads the Salesforce credentials from AWS Secrets Manager (cached for as
     long as the container stays warm).
  2. Asks Salesforce for an access token using the OAuth 2.0 client-credentials
     flow (cached for ten minutes, refreshed once if Salesforce answers 401).
  3. Runs a SOQL query for the Contact whose Phone or MobilePhone matches the
     caller, over the REST API. The query travels in the URL, so the phone
     number's ``+`` is sent as ``%2B``. A raw ``+`` in a URL query string means
     "space", and the lookup would silently find nobody.
  4. Returns a flat map of strings, exactly as Connect requires.

The two rules that shape the error handling
-------------------------------------------
  * Connect abandons a flow Lambda at 8 SECONDS. This function gives itself a
    total budget of 4 seconds and hands each outbound call whatever is left of
    it (never more than 2 seconds). The Lambda timeout in Terraform is the hard
    backstop. A socket timeout applies per operation, not to the whole call, so
    the budget is checked between calls rather than trusted blindly.
  * An unknown caller, a Salesforce outage and a bad credential are all
    handled and returned as a ``status`` plus a ``reason``. The function does
    not raise for them: the caller is still greeted ("there") and the flow
    carries on. A bug is the only thing that should take the Error branch.

Secrets
-------
The client secret and the access token are never logged, never returned and
never put in an exception message. The log carries a reason code and the last
four digits of the caller's number, nothing more.

Expected secret (JSON, in Secrets Manager)::

    {"client_id": "...", "client_secret": "...", "instance_url": "https://<org>.my.salesforce.com"}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SECRET_ID = os.environ.get("SALESFORCE_SECRET_ID", "")
API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v63.0")
CALL_TIMEOUT = float(os.environ.get("SALESFORCE_CALL_TIMEOUT_SEC", "2"))
TOTAL_BUDGET = float(os.environ.get("SALESFORCE_TOTAL_BUDGET_SEC", "4"))
TOKEN_TTL_SEC = 600

# No retries on the Secrets Manager call: a retry would spend the budget that
# the two Salesforce calls need. Worst case here is about 3 seconds, and it only
# happens on a cold container, because the secret is cached afterwards.
_CONFIG = Config(
    connect_timeout=1,
    read_timeout=2,
    retries={"max_attempts": 0, "mode": "standard"},
)

# Module scope so warm invocations reuse the connection and the caches below.
_secrets = boto3.client("secretsmanager", config=_CONFIG)
_cache: dict[str, Any] = {"secret": None, "token": None, "token_at": 0.0}


class _Fallback(Exception):  # noqa: N818 - control flow, not an error type
    """Internal: stop the lookup and return a safe answer instead."""

    def __init__(self, reason: str, status: str = "error") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _Unauthorized(Exception):  # noqa: N818 - control flow, not an error type
    """Internal: Salesforce answered 401, so the cached token is stale."""


def _response(**fields: Any) -> dict[str, str]:
    """Coerce to the flat string map Connect requires (None becomes "")."""
    out = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    if len(json.dumps(out).encode("utf-8")) > 32_000:
        return {"status": "error", "callerName": "there", "company": "", "reason": "too_large"}
    return out


def _fallback_response(status: str, reason: str) -> dict[str, str]:
    return _response(status=status, callerName="there", company="", reason=reason)


def _normalise(raw: str | None) -> str:
    """Reduce a caller number to E.164-ish form: digits and a leading plus."""
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if cleaned.startswith("0") and len(cleaned) == 10:  # 04xx xxx xxx
        cleaned = "+61" + cleaned[1:]
    return cleaned


def _phone_variants(e164: str) -> list[str]:
    """The ways an Australian number is commonly stored in a Salesforce field.

    Salesforce phone fields are free text, so an exact match only works on the
    format the record was typed in. Try E.164 and the national 0-prefixed form.
    """
    variants = [e164]
    if e164.startswith("+61") and len(e164) == 12:
        variants.append("0" + e164[3:])
    return variants


def _build_soql(variants: list[str]) -> str:
    # Values are validated as digits with an optional plus before they get here,
    # so nothing user-supplied reaches the query unchecked.
    if not all(re.fullmatch(r"\+?\d{6,15}", v) for v in variants):
        raise _Fallback("unusable_number", "not_found")
    values = ", ".join(f"'{v}'" for v in variants)
    # SOQL, and every value was just checked as digits only.
    soql = (
        "SELECT Id, Name, Account.Name FROM Contact "  # noqa: S608
        f"WHERE Phone IN ({values}) OR MobilePhone IN ({values}) LIMIT 1"
    )
    return soql


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0.05:
        raise _Fallback("budget_exhausted", "degraded")
    return min(CALL_TIMEOUT, left)


def _open(req: urllib.request.Request, timeout: float) -> Any:
    """The only place this module touches the network (tests replace it)."""
    # The scheme is https and the host ends in salesforce.com: _load_secret
    # refuses anything else before a request is ever built.
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def _is_timeout(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, TimeoutError)


def _load_secret(deadline: float) -> dict[str, str]:
    cached = _cache["secret"]
    if cached:
        return cached

    _remaining(deadline)  # refuse to start if the budget is already spent
    try:
        raw = _secrets.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    except (ClientError, BotoCoreError, KeyError) as exc:
        LOG.error("secret read failed: %s", type(exc).__name__)
        raise _Fallback("secret_unreachable") from None

    try:
        secret = json.loads(raw)
    except ValueError:
        # The usual cause is a missing pair of quotes around instance_url.
        LOG.error("secret is not valid JSON")
        raise _Fallback("secret_invalid_json") from None

    keys = ("client_id", "client_secret", "instance_url")
    if not isinstance(secret, dict) or not all(
        isinstance(secret.get(k), str) and secret[k] for k in keys
    ):
        LOG.error("secret is missing one of client_id, client_secret, instance_url")
        raise _Fallback("secret_missing_keys")

    instance_url = secret["instance_url"].rstrip("/")
    host = urllib.parse.urlparse(instance_url).hostname or ""
    if not instance_url.startswith("https://") or not host.endswith(
        (".salesforce.com", ".force.com")
    ):
        # Never send a client secret to a host that is not Salesforce.
        LOG.error("instance_url is not an https Salesforce address")
        raise _Fallback("instance_url_not_salesforce")

    secret["instance_url"] = instance_url
    _cache["secret"] = secret
    return secret


def _error_code(exc: urllib.error.HTTPError) -> str:
    """The short Salesforce error code (e.g. invalid_client), never the body."""
    try:
        code = json.loads(exc.read()).get("error", "")
    except (ValueError, AttributeError, OSError):
        return ""
    return code if isinstance(code, str) and len(code) < 60 else ""


def _get_token(secret: dict[str, str], deadline: float, *, force: bool = False) -> str:
    now = time.monotonic()
    if not force and _cache["token"] and now - _cache["token_at"] < TOKEN_TTL_SEC:
        return _cache["token"]

    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": secret["client_id"],
            "client_secret": secret["client_secret"],
        }
    ).encode("ascii")
    req = urllib.request.Request(  # noqa: S310 - host validated in _load_secret
        f"{secret['instance_url']}/services/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _open(req, _remaining(deadline)) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        LOG.error("token request rejected: http %s %s", exc.code, _error_code(exc))
        raise _Fallback(f"token_http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        timed_out = _is_timeout(exc)
        LOG.error("token request failed: %s", "timeout" if timed_out else type(exc).__name__)
        raise _Fallback("token_timeout" if timed_out else "token_network", "degraded") from None
    except ValueError:
        raise _Fallback("token_bad_response") from None

    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise _Fallback("token_bad_response")

    _cache["token"] = token
    _cache["token_at"] = time.monotonic()
    return token


def _query_contact(
    secret: dict[str, str], token: str, soql: str, deadline: float
) -> dict[str, Any] | None:
    # quote_via=quote encodes a space as %20 and a plus as %2B. The default
    # (quote_plus) would turn spaces into "+", which is a different thing.
    qs = urllib.parse.urlencode({"q": soql}, quote_via=urllib.parse.quote)
    req = urllib.request.Request(  # noqa: S310 - host validated in _load_secret
        f"{secret['instance_url']}/services/data/{API_VERSION}/query?{qs}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with _open(req, _remaining(deadline)) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise _Unauthorized from None
        LOG.error("query rejected: http %s %s", exc.code, _error_code(exc))
        raise _Fallback(f"query_http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        timed_out = _is_timeout(exc)
        LOG.error("query failed: %s", "timeout" if timed_out else type(exc).__name__)
        raise _Fallback("query_timeout" if timed_out else "query_network", "degraded") from None
    except ValueError:
        raise _Fallback("query_bad_response") from None

    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise _Fallback("query_bad_response")
    return records[0] if records else None


def _find_contact(
    secret: dict[str, str], variants: list[str], deadline: float
) -> dict[str, Any] | None:
    soql = _build_soql(variants)
    token = _get_token(secret, deadline)
    try:
        return _query_contact(secret, token, soql, deadline)
    except _Unauthorized:
        # The cached token expired or was revoked. Fetch a new one and try once
        # more; a second 401 means the credentials, not the cache, are the problem.
        _cache["token"] = None
        token = _get_token(secret, deadline, force=True)
        try:
            return _query_contact(secret, token, soql, deadline)
        except _Unauthorized:
            raise _Fallback("query_http_401") from None


def lambda_handler(event: dict, context: Any) -> dict[str, str]:
    """Entry point. Returns a flat string map on every path."""
    deadline = time.monotonic() + TOTAL_BUDGET

    details = event.get("Details", {})
    params = details.get("Parameters", {}) or {}
    contact = details.get("ContactData", {}) or {}
    endpoint = contact.get("CustomerEndpoint") or {}

    caller = _normalise(params.get("callerNumber") or endpoint.get("Address"))
    contact_id = contact.get("ContactId", "unknown")

    LOG.info(
        "lookup start %s",
        json.dumps({"contactId": contact_id, "caller": caller[-4:] if caller else ""}),
    )

    if not caller:
        # Withheld or blocked caller ID. Expected, not an error.
        return _fallback_response("not_found", "no_caller_id")

    if not re.fullmatch(r"\+?\d{6,15}", caller):
        return _fallback_response("not_found", "unusable_number")

    if not SECRET_ID:
        LOG.error("SALESFORCE_SECRET_ID is not set")
        return _fallback_response("error", "secret_env_missing")

    try:
        secret = _load_secret(deadline)
        record = _find_contact(secret, _phone_variants(caller), deadline)
    except _Fallback as fb:
        LOG.warning("lookup fell back for contact %s: %s", contact_id, fb.reason)
        return _fallback_response(fb.status, fb.reason)

    if record is None:
        LOG.info("no Salesforce contact for contact %s", contact_id)
        return _fallback_response("not_found", "no_match")

    account = record.get("Account") or {}
    return _response(
        status="found",
        callerName=record.get("Name") or "there",
        company=account.get("Name"),
        reason="",
    )
