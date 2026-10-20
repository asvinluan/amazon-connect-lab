"""Credential broker for Amazon Connect web chat.

Sits behind API Gateway. The browser POSTs here; this function calls
StartChatContact and returns ONLY the participant token.

Why this function has to exist
------------------------------
StartChatContact is a SigV4-signed IAM call. A browser cannot make it without
holding AWS credentials, and a browser must never hold AWS credentials. So the
call is made server-side by something that holds an IAM role, and the browser
is given a scoped, short-lived ParticipantToken instead. With that token it
talks to the connectparticipant API, which is unauthenticated by design.

    Browser -> API Gateway -> this Lambda -> StartChatContact
    Browser <---- ParticipantToken only ----+
       |
       +--> connectparticipant.CreateParticipantConnection (token, no IAM)

What this function must never do
--------------------------------
Return AWS credentials, the instance id, the contact flow id, or anything else
from the AWS side. Every one of those is useful to an attacker and useless to
the browser. The response below is deliberately three fields.

Why rate limiting matters here
------------------------------
Every successful call creates a real contact that occupies a real queue slot.
Unthrottled, one script fills the queue and real customers hear "we are
experiencing high call volume". The throttle on the API Gateway stage is
queue-flood protection, not generic web hygiene.
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

INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")
CONTACT_FLOW_ID = os.environ.get("CONTACT_FLOW_ID", "")

MAX_NAME_LEN = 64
MAX_MESSAGE_LEN = 1024

_connect = boto3.client(
    "connect",
    config=Config(
        connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"}
    ),
)

_CORS = {
    "Content-Type": "application/json",
    # Lock this to your real web origin before using this anywhere public.
    "Access-Control-Allow-Origin": "*",
}


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body)}


def _clean(value: Any, limit: int) -> str:
    """Trim and bound a user-supplied string.

    Length bounds are not cosmetic: DisplayName and the initial message are
    passed to a Connect API that rejects oversized values, and an unbounded
    string from the internet is an easy way to make every call fail.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def lambda_handler(event: dict, context: Any) -> dict:
    if not INSTANCE_ID or not CONTACT_FLOW_ID:
        LOG.error("CONNECT_INSTANCE_ID or CONTACT_FLOW_ID is not configured")
        return _reply(500, {"error": "service_misconfigured"})

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _reply(400, {"error": "invalid_json"})

    if not isinstance(payload, dict):
        return _reply(400, {"error": "invalid_json"})

    name = _clean(payload.get("name"), MAX_NAME_LEN) or "Guest"
    message = _clean(payload.get("message"), MAX_MESSAGE_LEN) or "Chat started"

    try:
        result = _connect.start_chat_contact(
            InstanceId=INSTANCE_ID,
            ContactFlowId=CONTACT_FLOW_ID,
            ParticipantDetails={"DisplayName": name},
            InitialMessage={"ContentType": "text/plain", "Content": message},
            Attributes={
                "channel": "web",
                # Useful for tracing a session back through the CTR stream.
                "sourceRequestId": event.get("requestContext", {}).get("requestId", ""),
            },
        )

    except _connect.exceptions.LimitExceededException:
        # The instance hit a concurrency or chat quota. 503 with a retry hint
        # is correct here: the request was valid and the service is full.
        LOG.warning("Connect limit exceeded starting chat")
        return _reply(503, {"error": "at_capacity", "retryAfterSeconds": 30})

    except (ClientError, BotoCoreError) as exc:
        # Log the detail, return none of it. Internal error text leaking to a
        # public endpoint tells an attacker about your configuration.
        LOG.exception("StartChatContact failed: %s", exc)
        return _reply(502, {"error": "chat_unavailable"})

    LOG.info("chat started contactId=%s", result.get("ContactId"))

    # Three fields. Nothing about the AWS account goes back to the browser.
    return _reply(
        200,
        {
            "contactId": result["ContactId"],
            "participantId": result["ParticipantId"],
            "participantToken": result["ParticipantToken"],
        },
    )
