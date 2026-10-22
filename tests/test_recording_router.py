"""Unit tests for the S3 recording router.

DynamoDB is a MagicMock, so nothing here touches AWS. The tests pin the three
properties that make an event-driven function safe:

  1. A duplicate delivery is recognised and skipped, never an error.
  2. A genuine failure (malformed event, DynamoDB error) RAISES, so Lambda
     retries and then dead-letters the event.
  3. The object key is URL-decoded before it is used.

Run:  pytest -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

HANDLER = Path(__file__).resolve().parents[1] / "lambda" / "recording_router" / "handler.py"


@pytest.fixture()
def router(monkeypatch):
    """A freshly imported handler wired to a fake DynamoDB client."""
    monkeypatch.setenv("EVENTS_TABLE", "test-events")
    spec = importlib.util.spec_from_file_location("recording_router_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake = MagicMock()
    module._ddb = fake
    return module, fake


def _record(key: str = "incoming/call-1.wav", etag: str = "abc123", size: int = 2048) -> dict:
    return {
        "eventName": "ObjectCreated:Put",
        "eventTime": "2026-10-23T01:02:03.000Z",
        "s3": {
            "bucket": {"name": "test-bucket"},
            "object": {"key": key, "size": size, "eTag": etag},
        },
    }


def _event(*records: dict) -> dict:
    return {"Records": list(records)}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "PutItem")


def test_new_object_is_recorded(router):
    module, fake = router

    result = module.lambda_handler(_event(_record()), None)

    assert result == {"recorded": 1, "duplicate": 0, "skipped": 0}
    kwargs = fake.put_item.call_args.kwargs
    assert kwargs["TableName"] == "test-events"
    assert kwargs["Item"]["objectKey"] == {"S": "incoming/call-1.wav"}
    assert kwargs["Item"]["sizeBytes"] == {"N": "2048"}
    assert kwargs["Item"]["status"] == {"S": "received"}


def test_write_is_conditional_so_duplicates_cannot_overwrite(router):
    module, fake = router

    module.lambda_handler(_event(_record()), None)

    assert fake.put_item.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(pk)"


def test_idempotency_key_includes_the_etag(router):
    module, fake = router

    module.lambda_handler(_event(_record(etag="v1")), None)
    module.lambda_handler(_event(_record(etag="v2")), None)

    keys = [c.kwargs["Item"]["pk"]["S"] for c in fake.put_item.call_args_list]
    assert keys == ["test-bucket/incoming/call-1.wav#v1", "test-bucket/incoming/call-1.wav#v2"]


def test_duplicate_delivery_is_not_an_error(router):
    """S3 can deliver the same event twice. The second one must be skipped quietly."""
    module, fake = router
    fake.put_item.side_effect = _client_error("ConditionalCheckFailedException")

    result = module.lambda_handler(_event(_record()), None)

    assert result == {"recorded": 0, "duplicate": 1, "skipped": 0}


def test_url_encoded_key_is_decoded(router):
    """A space arrives as '+' and a plus as %2B."""
    module, fake = router

    module.lambda_handler(_event(_record(key="incoming/call+one%2Btwo.wav")), None)

    stored = fake.put_item.call_args.kwargs["Item"]["objectKey"]["S"]
    assert stored == "incoming/call one+two.wav"


def test_key_outside_the_watched_prefix_is_skipped_not_failed(router):
    module, fake = router

    result = module.lambda_handler(_event(_record(key="other/file.wav")), None)

    assert result == {"recorded": 0, "duplicate": 0, "skipped": 1}
    fake.put_item.assert_not_called()


def test_several_records_in_one_event_are_all_handled(router):
    module, fake = router

    result = module.lambda_handler(
        _event(_record(key="incoming/a.wav"), _record(key="incoming/b.wav")), None
    )

    assert result["recorded"] == 2
    assert fake.put_item.call_count == 2


@pytest.mark.parametrize("bad", [{}, {"Records": []}, {"Records": "nope"}, None])
def test_malformed_event_raises_so_it_is_retried_and_dead_lettered(router, bad):
    module, _ = router

    with pytest.raises(ValueError):
        module.lambda_handler(bad, None)


def test_record_that_is_not_an_s3_record_raises(router):
    module, fake = router

    with pytest.raises(ValueError):
        module.lambda_handler({"Records": [{"unexpected": "shape"}]}, None)

    fake.put_item.assert_not_called()


def test_dynamodb_failure_raises_so_lambda_retries(router):
    """Throttling or a permissions error is a real failure: it must not be swallowed."""
    module, fake = router
    fake.put_item.side_effect = _client_error("ProvisionedThroughputExceededException")

    with pytest.raises(ClientError):
        module.lambda_handler(_event(_record()), None)


def test_missing_table_setting_raises(router, monkeypatch):
    module, _ = router
    monkeypatch.setattr(module, "TABLE_NAME", "")

    with pytest.raises(RuntimeError):
        module.lambda_handler(_event(_record()), None)


def test_row_expires_so_the_table_cannot_grow_forever(router):
    module, fake = router

    module.lambda_handler(_event(_record()), None)

    expires = int(fake.put_item.call_args.kwargs["Item"]["expiresAt"]["N"])
    assert expires > 0
