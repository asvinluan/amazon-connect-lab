# ---------------------------------------------------------------------------
# Observability.
#
# Four alarms, each tied to a failure this lab can actually produce. An alarm
# that cannot fire teaches nothing and is worse than no alarm, because it
# looks like coverage.
#
# Metric names and dimensions verified against the Amazon Connect CloudWatch
# documentation: namespace AWS/Connect, dimensions InstanceId, MetricGroup,
# ContactFlowName.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name = "${var.instance_alias}-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
  # Note: an email subscription stays "pending confirmation" until you click
  # the link in the confirmation email. Terraform reports success either way.
}

# ---------------------------------------------------------------------------
# 1. Contact flow errors
#
# Fires when a flow takes an Error branch. The usual causes are a Lambda that
# timed out or returned badly, or a block hitting a condition the flow does
# not handle.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "contact_flow_errors" {
  alarm_name        = "${var.instance_alias}-contact-flow-errors"
  alarm_description = "A contact flow took an Error branch. Check flow logs for the block that failed."

  namespace   = "AWS/Connect"
  metric_name = "ContactFlowErrors"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    InstanceId      = aws_connect_instance.lab.id
    MetricGroup     = "ContactFlow"
    ContactFlowName = aws_connect_contact_flow.main.name
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching" # no calls is not a failure

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# 2. Contact flow FATAL errors
#
# Different metric, more serious: the flow failed to execute at all rather
# than branching to Error. Treated as a lower threshold and its own alarm so
# it is not lost in the noise of ordinary error branches.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "contact_flow_fatal" {
  alarm_name        = "${var.instance_alias}-contact-flow-fatal-errors"
  alarm_description = "A contact flow failed to execute due to a system error. Callers are affected right now."

  namespace   = "AWS/Connect"
  metric_name = "ContactFlowFatalErrors"
  statistic   = "Sum"
  period      = 60

  dimensions = {
    InstanceId      = aws_connect_instance.lab.id
    MetricGroup     = "ContactFlow"
    ContactFlowName = aws_connect_contact_flow.main.name
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# 3. Misconfigured phone numbers
#
# This one is personal. It counts calls that failed because the number is not
# associated with a flow -- which is exactly what the caller hears as
# "the number you have called is unavailable".
#
# Nothing in the Connect console tells you this is happening. Without this
# alarm you find out when a customer complains.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "misconfigured_numbers" {
  alarm_name        = "${var.instance_alias}-misconfigured-phone-numbers"
  alarm_description = "A call hit a phone number with no flow attached. Callers hear 'the number you have called is unavailable'."

  namespace   = "AWS/Connect"
  metric_name = "MisconfiguredPhoneNumbers"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    InstanceId  = aws_connect_instance.lab.id
    MetricGroup = "VoiceCalls"
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# 4. Lambda duration approaching the Connect ceiling
#
# Connect abandons a flow Lambda invocation at 8 seconds and takes the Error
# branch. Alarming on the function's own Errors metric tells you it already
# broke; alarming on p99 duration tells you it is about to.
#
# Extended statistic p99, not Average: an average hides the tail, and the
# tail is what times out.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_slow" {
  alarm_name        = "${var.instance_alias}-caller-lookup-slow"
  alarm_description = "Caller lookup p99 duration is approaching Connect's 8-second invocation ceiling."

  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  extended_statistic  = "p99"
  period              = 300

  dimensions = {
    FunctionName = aws_lambda_function.caller_lookup.function_name
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.lambda_duration_alarm_ms
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name        = "${var.instance_alias}-caller-lookup-errors"
  alarm_description = "Caller lookup is throwing. The flow will be taking its Error branch."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  period      = 300

  dimensions = {
    FunctionName = aws_lambda_function.caller_lookup.function_name
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# A dashboard, so there is one place to look during a test call.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "lab" {
  dashboard_name = "${var.instance_alias}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Contact flow errors"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/Connect", "ContactFlowErrors", "InstanceId", aws_connect_instance.lab.id,
            "MetricGroup", "ContactFlow", "ContactFlowName", aws_connect_contact_flow.main.name],
            [".", "ContactFlowFatalErrors", ".", ".", ".", ".", ".", "."],
          ]
          stat = "Sum", period = 300
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Caller lookup duration (p50 / p99) vs Connect 8s ceiling"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.caller_lookup.function_name,
            { stat = "p50" }],
            ["...", { stat = "p99" }],
          ]
          period = 300
          annotations = {
            horizontal = [
              { label = "Connect invoke ceiling", value = 8000 },
              { label = "Alarm threshold", value = var.lambda_duration_alarm_ms },
            ]
          }
        }
      },
    ]
  })
}
