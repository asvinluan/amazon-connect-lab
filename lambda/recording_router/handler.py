"""Recording router: reacts to a file landing in S3.

This is the event-driven part of the lab. Nothing calls this function. A call
recording (or any file) is written to the bucket under ``incoming/``, S3 emits
an ``ObjectCreated`` event, and Lambda runs. The function records that the file
arrived in a DynamoDB table. In a real contact centre the same trigger would
start transcription, redaction or archiving; the plumbing around it is the
point of this lab, not the business logic.

Three properties that make an event-driven function safe to run
---------------------------------------------------------------
  1. IT MUST BE IDEMPOTENT. S3 delivers events at least once, so the same event
     can arrive twice. Each arrival is written with a conditional put keyed on
     the object key plus its ETag. The second delivery is recognised as a
     duplicate and skipped, not written again and not treated as an error.

  2. IT MUST FAIL LOUDLY WHEN IT CANNOT DO ITS JOB. S3 invokes Lambda
     asynchronously, so there is no caller to hand an error back to. If this
     function raises, Lambda retries it (twice, set in Terraform) and then
     parks the event in an SQS dead-letter queue, where a CloudWatch alarm
     fires. An expected condition (a duplicate, a key outside ``incoming/``)
     is NOT a failure and must not raise, or it would retry and land in the
     dead-letter queue for no reason. A malformed event or a DynamoDB error IS
     a failure and does raise.

  3. IT MUST DECODE THE KEY. S3 URL-encodes object keys in the event: a space
     arrives as ``+`` and other characters as ``%XX``. Using the raw key would
     record a name that does not exist in the bucket.

The function never reads the object itself. It works from the event metadata
only, so its role needs no S3 or KMS permission at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("EVENTS_TABLE", "")
PREFIX = os.environ.get("WATCH_PREFIX", "incoming/")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

# 2-second timeouts and one retry: a hung DynamoDB call should fail fast so
# Lambda's own retry and the dead-letter queue take over.
_CONFIG = Config(
    connect_timeout=2,
    read_timeout=2,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Module scope so warm invocations reuse the connection.
_ddb = boto3.client("dynamodb", config=_CONFIG)


def _decode_key(raw: str) -> str:
    """S3 sends keys URL-encoded: '+' means a space and '%XX' is an escape."""
    return urllib.parse.unquote_plus(raw)


def _record_arrival(record: dict[str, Any]) -> str:
    """Write one S3 event record. Returns "recorded", "duplicate" or "skipped"."""
    try:
        s3 = record["s3"]
        bucket = s3["bucket"]["name"]
        raw_key = s3["object"]["key"]
    except (KeyError, TypeError) as exc:
        # Not shaped like an S3 event. Raise so Lambda retries and then
        # dead-letters it: a person needs to look at this one.
        raise ValueError("record is not an S3 event record") from exc

    key = _decode_key(raw_key)
    obj = s3["object"]
    etag = str(obj.get("eTag", ""))
    size = int(obj.get("size", 0))

    if not key.startswith(PREFIX):
        # The bucket notification already filters on this prefix, so reaching
        # here means the filter changed. Expected condition, not a failure.
        LOG.warning("ignoring key outside %s: %s", PREFIX, key)
        return "skipped"

    now = time.time()
    item = {
        # Idempotency key: the same object content delivered twice collapses
        # to one row; the same key with new content is a new row.
        "pk": {"S": f"{bucket}/{key}#{etag}"},
        "objectKey": {"S": key},
        "bucket": {"S": bucket},
        "sizeBytes": {"N": str(size)},
        "eventName": {"S": str(record.get("eventName", ""))},
        "eventTime": {"S": str(record.get("eventTime", ""))},
        "receivedAt": {"S": datetime.now(UTC).isoformat()},
        "status": {"S": "received"},
        # DynamoDB TTL removes the row later, so the lab table never grows.
        "expiresAt": {"N": str(int(now) + RETENTION_DAYS * 86_400)},
    }

    try:
        _ddb.put_item(
            TableName=TABLE_NAME,
            Item=item,
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            LOG.info("duplicate delivery ignored: %s", key)
            return "duplicate"
        # Throttling, permissions, a missing table: a real failure. Raise.
        LOG.error("DynamoDB write failed: %s", code)
        raise

    LOG.info("recorded arrival: %s (%d bytes)", key, size)
    return "recorded"


def lambda_handler(event: dict, context: Any) -> dict[str, Any]:
    """Entry point. Raises on a malformed event or a failed write."""
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("event has no Records list")

    if not TABLE_NAME:
        raise RuntimeError("EVENTS_TABLE is not set")

    counts = {"recorded": 0, "duplicate": 0, "skipped": 0}
    for record in records:
        counts[_record_arrival(record)] += 1

    LOG.info("batch done %s", json.dumps(counts))
    return counts
