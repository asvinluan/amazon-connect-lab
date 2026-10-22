# ---------------------------------------------------------------------------
# AI assist: a guarded generative answer.
#
# A Lambda calls a foundation model through the Bedrock Converse API with a
# guardrail attached. The guardrail is created first, published as a numbered
# version, and the Lambda is pinned to that version, so what is tested is what
# runs. Three metric filters and alarms watch the Lambda's own log lines.
#
# Nothing here is called by the voice path. Test it from the Lambda console or
# with scripts/run_ai_eval.py.
# ---------------------------------------------------------------------------

locals {
  # An inference profile ID looks like "apac.amazon.nova-lite-v1:0". The
  # foundation model behind it is the same string without the "apac." prefix.
  foundation_model_id = replace(var.bedrock_model_id, "/^[a-z]+\\./", "")
  safe_message        = "Sorry, I can't help with that here. I can connect you to an agent."
}

data "archive_file" "ai_assist" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/ai_assist"
  output_path = "${path.module}/build/ai_assist.zip"
}

# ---------------------------------------------------------------- guardrail

resource "aws_bedrock_guardrail" "ai_assist" {
  name                      = "${var.instance_alias}-ai-assist"
  description               = "Denies financial and legal advice, filters harmful content and prompt attacks, blocks card numbers."
  blocked_input_messaging   = local.safe_message
  blocked_outputs_messaging = local.safe_message

  topic_policy_config {
    topics_config {
      name       = "financial_advice"
      type       = "DENY"
      definition = "Recommendations about shares, investments, savings products, loans or how to invest or spend money."
      examples = [
        "Which stock should I buy this week?",
        "Should I put my savings into a term deposit?",
      ]
    }
    topics_config {
      name       = "legal_advice"
      type       = "DENY"
      definition = "Advice about legal rights, contracts, disputes, lawsuits or what to say in court or a tribunal."
      examples = [
        "Can I sue my landlord?",
        "Is my employment contract legal?",
      ]
    }
  }

  content_policy_config {
    filters_config {
      type            = "HATE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "INSULTS"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "VIOLENCE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "MISCONDUCT"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    # Prompt attacks are an input-side control. Output strength must be NONE.
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "CREDIT_DEBIT_CARD_NUMBER"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "EMAIL"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "PHONE"
      action = "ANONYMIZE"
    }
  }
}

# A numbered, immutable snapshot of the guardrail. The Lambda uses this, never
# DRAFT. To ship a change: edit the guardrail above, change the description
# below, apply, and the Lambda moves to the new number.
resource "aws_bedrock_guardrail_version" "ai_assist" {
  guardrail_arn = aws_bedrock_guardrail.ai_assist.guardrail_arn
  description   = "v1: two denied topics, five content filters, three PII entities"
}

# ----------------------------------------------------------------- the Lambda

resource "aws_cloudwatch_log_group" "ai_assist" {
  name              = "/aws/lambda/${var.instance_alias}-ai-assist"
  retention_in_days = 14
}

resource "aws_iam_role" "ai_assist" {
  name = "${var.instance_alias}-ai-assist"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "ai_assist" {
  name = "ai-assist-policy"
  role = aws_iam_role.ai_assist.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.ai_assist.arn}:*"
      },
      {
        # One model, through one inference profile. The foundation model ARN
        # has a wildcard region because a cross-region profile may run the
        # request in any region of its geography.
        Sid    = "InvokeOneModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:*::foundation-model/${local.foundation_model_id}",
        ]
      },
      {
        # Converse needs this to evaluate the attached guardrail.
        Sid      = "ApplyOneGuardrail"
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = aws_bedrock_guardrail.ai_assist.guardrail_arn
      },
    ]
  })
}

resource "aws_lambda_function" "ai_assist" {
  function_name = "${var.instance_alias}-ai-assist"
  role          = aws_iam_role.ai_assist.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.ai_assist.output_path
  source_code_hash = data.archive_file.ai_assist.output_base64sha256

  # The handler's own client gives up after 5 seconds. This is the backstop,
  # under Connect's 8-second ceiling (see the lambda_timeout_seconds variable).
  timeout     = 7
  memory_size = 256

  environment {
    variables = {
      MODEL_ID          = var.bedrock_model_id
      GUARDRAIL_ID      = aws_bedrock_guardrail.ai_assist.guardrail_id
      GUARDRAIL_VERSION = aws_bedrock_guardrail_version.ai_assist.version
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.ai_assist,
    aws_iam_role_policy.ai_assist,
  ]
}

# ------------------------------------------------- model invocation logging
#
# Bedrock's own record of every model call, separate from the Lambda's log.
# It is one setting per account and region, so enabling it here changes it for
# anything else in the account that uses Bedrock.

resource "aws_cloudwatch_log_group" "bedrock_invocations" {
  count = var.enable_model_invocation_logging ? 1 : 0

  name              = "/aws/bedrock/${var.instance_alias}-invocations"
  retention_in_days = 14
}

resource "aws_iam_role" "bedrock_logging" {
  count = var.enable_model_invocation_logging ? 1 : 0

  name = "${var.instance_alias}-bedrock-logging"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "bedrock_logging" {
  count = var.enable_model_invocation_logging ? 1 : 0

  name = "bedrock-logging-policy"
  role = aws_iam_role.bedrock_logging[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.bedrock_invocations[0].arn}:*"
    }]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  count = var.enable_model_invocation_logging ? 1 : 0

  logging_config {
    text_data_delivery_enabled      = true
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.bedrock_invocations[0].name
      role_arn       = aws_iam_role.bedrock_logging[0].arn
    }
  }

  depends_on = [aws_iam_role_policy.bedrock_logging]
}

# --------------------------------------------- metrics and alarms from the logs
#
# The handler prints one JSON line per call. These filters turn the lines into
# metrics, so an AI-specific failure (a spike in blocked prompts, model errors,
# slow answers) raises an alarm like any other fault.

resource "aws_cloudwatch_log_metric_filter" "ai_blocked" {
  name           = "${var.instance_alias}-ai-blocked"
  log_group_name = aws_cloudwatch_log_group.ai_assist.name
  pattern        = "{ $.event = \"ai_assist\" && $.status = \"blocked\" }"

  metric_transformation {
    namespace = "LabAiAssist"
    name      = "BlockedPrompts"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "ai_errors" {
  name           = "${var.instance_alias}-ai-errors"
  log_group_name = aws_cloudwatch_log_group.ai_assist.name
  pattern        = "{ $.event = \"ai_assist\" && $.status = \"error\" }"

  metric_transformation {
    namespace = "LabAiAssist"
    name      = "ModelErrors"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "ai_latency" {
  name           = "${var.instance_alias}-ai-latency"
  log_group_name = aws_cloudwatch_log_group.ai_assist.name
  pattern        = "{ $.event = \"ai_assist\" && $.latency_ms = * }"

  metric_transformation {
    namespace = "LabAiAssist"
    name      = "AnswerLatencyMs"
    value     = "$.latency_ms"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_metric_alarm" "ai_blocked_spike" {
  alarm_name        = "${var.instance_alias}-ai-blocked-spike"
  alarm_description = "Five or more prompts were blocked by the guardrail in five minutes. Either an attack is under way or the guardrail is too strict."

  namespace   = "LabAiAssist"
  metric_name = "BlockedPrompts"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 5
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.ai_blocked]
}

resource "aws_cloudwatch_metric_alarm" "ai_errors" {
  alarm_name        = "${var.instance_alias}-ai-errors"
  alarm_description = "The AI assist could not get an answer from the model (timeout, throttling or permissions). Callers heard the fallback sentence."

  namespace   = "LabAiAssist"
  metric_name = "ModelErrors"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.ai_errors]
}

resource "aws_cloudwatch_metric_alarm" "ai_slow" {
  alarm_name        = "${var.instance_alias}-ai-slow"
  alarm_description = "The 95th percentile answer time is over 4 seconds. A caller is waiting on this, and Connect gives up at 8 seconds."

  namespace          = "LabAiAssist"
  metric_name        = "AnswerLatencyMs"
  extended_statistic = "p95"
  period             = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 4000
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.ai_latency]
}
