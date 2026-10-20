"""Caller lookup for an Amazon Connect contact flow.

Invoked directly by the flow's "Invoke AWS Lambda function" block. Not behind
API Gateway -- Connect calls Lambda itself.

Three Connect constraints shape every design decision in this file, and none
of them are obvious from the Lambda side:

  1. Connect abandons the invocation at 8 SECONDS and takes the flow's Error
     branch. The function's own timeout is irrelevant to that. So every
     downstream call needs its own budget well under 8s, and the function has
     to return something usable rather than hanging.

  2. The response must be a FLAT MAP OF STRINGS, under 32 KB. Nested objects,
     numbers and booleans are rejected or silently coerced. Everything is
     str() on the way out.

  3. The function must NOT raise for an expected condition like "caller not
     found". Raising sends the flow down the Error branch, which is for
     "something broke", not for "we looked and there was nobody". An unknown
     caller is a successful lookup with a negative answer.

The flow reads the result from $.External, which holds ONLY the most recent
Lambda response -- a second invoke erases the first one's keys. That is why
the flow copies $.External into contact attributes immediately after this
block returns.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("CUSTOMER_TABLE", "")
DDB_TIMEOUT = float(os.environ.get("DDB_TIMEOUT_SEC", "2"))

# The timeout and retry budget is the whole point. Defaults would allow
# roughly 60 seconds of connect time plus retries, which blows straight
# through Connect's 8-second ceiling and guarantees the Error branch.
#
# 2s connect, 2s read, 1 retry  ->  worst case about 4s, leaving headroom.
_CONFIG = Config(
    connect_timeout=DDB_TIMEOUT,
    read_timeout=DDB_TIMEOUT,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Created at module scope so the connection is reused across warm invocations.
_ddb = boto3.client("dynamodb", config=_CONFIG)


def _response(**fields: Any) -> dict[str, str]:
    """Coerce to the flat string map Connect requires.

    None becomes an empty string rather than the literal "None", which would
    otherwise reach the caller as text in a prompt.
    """
    out = {k: ("" if v is None else str(v)) for k, v in fields.items()}

    encoded = len(json.dumps(out).encode("utf-8"))
    if encoded > 32_000:  # 32 KB ceiling, with room to spare
        LOG.error("Response %d bytes exceeds the Connect limit; truncating", encoded)
        return {"status": "error", "reason": "response_too_large"}

    return out


def _normalise(raw: str | None) -> str:
    """Reduce a caller number to E.164-ish digits for lookup.

    Connect gives the customer number in E.164 already, but a number typed
    into a test event may not be, and a lookup that fails on formatting is
    indistinguishable from a caller who is genuinely unknown.
    """
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if cleaned.startswith("0") and len(cleaned) == 10:  # 04xx xxx xxx
        cleaned = "+61" + cleaned[1:]
    return cleaned


def lambda_handler(event: dict, context: Any) -> dict[str, str]:
    """Entry point.

    Returns a flat string map in every path. The only way this raises is a
    bug, and a bug SHOULD take the Error branch -- that is what it is for.
    """
    # Connect nests the caller number several levels down. Reach for it
    # defensively: a test invocation from the console has a different shape
    # and should not crash the function.
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
        return _response(status="not_found", callerName="there", segment="standard")

    if not TABLE_NAME:
        LOG.error("CUSTOMER_TABLE is not set")
        return _response(status="error", callerName="there", segment="standard")

    try:
        result = _ddb.get_item(
            TableName=TABLE_NAME,
            Key={"phoneNumber": {"S": caller}},
            # Strongly consistent reads cost more and are not needed: a
            # customer record that is a few hundred milliseconds stale is
            # fine, and an eventually consistent read is faster, which
            # matters against an 8-second ceiling.
            ConsistentRead=False,
        )

    except _ddb.exceptions.ProvisionedThroughputExceededException:
        # Throttled. Degrade rather than fail -- the caller still gets
        # through, just without their name.
        LOG.warning("DynamoDB throttled for contact %s", contact_id)
        return _response(status="degraded", callerName="there", segment="standard")

    except (ClientError, BotoCoreError) as exc:
        # BotoCoreError covers the timeouts configured above. Connect would
        # otherwise sit waiting until its own 8-second cut-off, so catching
        # this is what keeps the caller moving.
        LOG.exception("DynamoDB lookup failed for contact %s: %s", contact_id, exc)
        return _response(status="error", callerName="there", segment="standard")

    item = result.get("Item")
    if not item:
        LOG.info("no record for contact %s", contact_id)
        return _response(status="not_found", callerName="there", segment="standard")

    return _response(
        status="found",
        callerName=item.get("callerName", {}).get("S", "there"),
        segment=item.get("segment", {}).get("S", "standard"),
    )
