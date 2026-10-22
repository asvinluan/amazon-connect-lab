# ---------------------------------------------------------------------------
# Event-driven recording router.
#
# Nothing in this file is called by anything. A file lands in the bucket under
# incoming/, S3 emits an ObjectCreated event, and Lambda runs:
#
#   S3 (incoming/)  -->  Lambda recording_router  -->  DynamoDB (one row per file)
#                              |
#                              +-- fails after 2 retries --> SQS dead-letter queue
#                                                              --> CloudWatch alarm --> SNS
#
# Why the pieces are shaped this way:
#
#   * S3 invokes Lambda ASYNCHRONOUSLY, so there is no caller to return an
#     error to. Retries plus a dead-letter queue are how a failure gets seen.
#   * S3 delivers events at least once, so the Lambda writes idempotently
#     (see lambda/recording_router/handler.py).
#   * The bucket is private, versioned and encrypted with a customer-managed
#     key. Files expire after 7 days so a lab cannot quietly accumulate cost.
# ---------------------------------------------------------------------------

data "archive_file" "recording_router" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/recording_router"
  output_path = "${path.module}/build/recording_router.zip"
}

# ---------------------------------------------------------------------------
# Storage: encrypted private bucket, and one DynamoDB row per arrival.
# ---------------------------------------------------------------------------

resource "aws_kms_key" "events" {
  description             = "KMS key for the recording bucket"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "events" {
  name          = "alias/${var.instance_alias}-events"
  target_key_id = aws_kms_key.events.key_id
}

resource "aws_s3_bucket" "recordings" {
  bucket = "${var.instance_alias}-recordings-${data.aws_caller_identity.current.account_id}"

  # Lab only: lets terraform destroy remove a bucket that still holds test files.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "recordings" {
  bucket                  = aws_s3_bucket.recordings.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.events.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  rule {
    id     = "expire-test-files"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.recordings]
}

resource "aws_dynamodb_table" "recording_events" {
  name         = "${var.instance_alias}-recording-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Rows delete themselves (the Lambda sets expiresAt), so the table stays tiny.
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false # lab only; enable for anything holding real records
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# The dead-letter queue: where an event waits after the Lambda has failed it
# three times (the first try plus two retries).
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "recording_dlq" {
  name                      = "${var.instance_alias}-recording-router-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum, so nothing is lost over a long weekend
  sqs_managed_sse_enabled   = true
}

# ---------------------------------------------------------------------------
# recording_router: role, log group, function, retries, and the S3 trigger.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "recording_router" {
  name              = "/aws/lambda/${var.instance_alias}-recording-router"
  retention_in_days = 14
}

resource "aws_iam_role" "recording_router" {
  name = "${var.instance_alias}-recording-router"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "recording_router" {
  name = "recording-router-policy"
  role = aws_iam_role.recording_router.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.recording_router.arn}:*"
      },
      {
        Sid      = "RecordArrival"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.recording_events.arn
      },
      {
        Sid      = "DeadLetter"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.recording_dlq.arn
      },
    ]
  })
}

resource "aws_lambda_function" "recording_router" {
  function_name = "${var.instance_alias}-recording-router"
  role          = aws_iam_role.recording_router.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.recording_router.output_path
  source_code_hash = data.archive_file.recording_router.output_base64sha256

  timeout     = 10
  memory_size = 128

  # Where an event goes after the retries below are used up.
  dead_letter_config {
    target_arn = aws_sqs_queue.recording_dlq.arn
  }

  environment {
    variables = {
      EVENTS_TABLE = aws_dynamodb_table.recording_events.name
      WATCH_PREFIX = "incoming/"
      LOG_LEVEL    = "INFO"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.recording_router,
    aws_iam_role_policy.recording_router,
  ]
}

# Asynchronous invocation settings. Two retries is the default; it is written
# out so the behaviour is visible in code review rather than implied.
resource "aws_lambda_function_event_invoke_config" "recording_router" {
  function_name                = aws_lambda_function.recording_router.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

resource "aws_lambda_permission" "s3_invoke_recording_router" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.recording_router.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.recordings.arn
  source_account = data.aws_caller_identity.current.account_id
}

resource "aws_s3_bucket_notification" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.recording_router.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incoming/"
  }

  # S3 checks it may invoke the function when this is created, so the
  # permission has to exist first.
  depends_on = [aws_lambda_permission.s3_invoke_recording_router]
}

# ---------------------------------------------------------------------------
# An alarm on the dead-letter queue. A message in here means a recording was
# NOT processed and nobody was told, which is the failure this alarm is for.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "recording_dlq_not_empty" {
  alarm_name        = "${var.instance_alias}-recording-dlq-not-empty"
  alarm_description = "An S3 event failed all retries and is waiting in the dead-letter queue. A recording was not processed."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 300

  dimensions = {
    QueueName = aws_sqs_queue.recording_dlq.name
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}
