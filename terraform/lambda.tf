# ---------------------------------------------------------------------------
# Two Lambda functions, because Connect reaches Lambda two different ways and
# conflating them is a common mistake:
#
#   caller_lookup  -- invoked DIRECTLY by the contact flow. No API Gateway
#                     involved. Connect calls Lambda itself.
#
#   chat_broker    -- invoked by API Gateway, from a browser. This one exists
#                     because StartChatContact is a SigV4-signed IAM call that
#                     a browser cannot make without holding AWS credentials.
#                     The Lambda holds the credentials and hands back only a
#                     short-lived participant token.
# ---------------------------------------------------------------------------

data "archive_file" "caller_lookup" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/caller_lookup"
  output_path = "${path.module}/build/caller_lookup.zip"
}

data "archive_file" "chat_broker" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/chat_broker"
  output_path = "${path.module}/build/chat_broker.zip"
}

# ---------------------------------------------------------------------------
# caller_lookup
# ---------------------------------------------------------------------------

resource "aws_iam_role" "caller_lookup" {
  name = "${var.instance_alias}-caller-lookup"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "caller_lookup" {
  name = "caller-lookup-policy"
  role = aws_iam_role.caller_lookup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.caller_lookup.arn}:*"
      },
      {
        Sid    = "ReadCustomers"
        Effect = "Allow"
        Action = ["dynamodb:GetItem"]
        # Scoped to one table and one action. No dynamodb:*, no Resource "*".
        Resource = aws_dynamodb_table.customers.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "caller_lookup" {
  name              = "/aws/lambda/${var.instance_alias}-caller-lookup"
  retention_in_days = 14
}

resource "aws_lambda_function" "caller_lookup" {
  function_name = "${var.instance_alias}-caller-lookup"
  role          = aws_iam_role.caller_lookup.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.caller_lookup.output_path
  source_code_hash = data.archive_file.caller_lookup.output_base64sha256

  # Under Connect's 8-second ceiling -- see the variable validation.
  timeout     = var.lambda_timeout_seconds
  memory_size = 256

  environment {
    variables = {
      CUSTOMER_TABLE  = aws_dynamodb_table.customers.name
      DDB_TIMEOUT_SEC = "2"
      LOG_LEVEL       = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.caller_lookup]
}

# ---------------------------------------------------------------------------
# chat_broker
# ---------------------------------------------------------------------------

resource "aws_iam_role" "chat_broker" {
  name = "${var.instance_alias}-chat-broker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "chat_broker" {
  name = "chat-broker-policy"
  role = aws_iam_role.chat_broker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.chat_broker.arn}:*"
      },
      {
        Sid    = "StartChat"
        Effect = "Allow"
        Action = ["connect:StartChatContact"]
        # Scoped to this instance's contact flows only. This is the entire
        # AWS entitlement the public internet can reach through this API.
        Resource = "${aws_connect_instance.lab.arn}/contact-flow/*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "chat_broker" {
  name              = "/aws/lambda/${var.instance_alias}-chat-broker"
  retention_in_days = 14
}

resource "aws_lambda_function" "chat_broker" {
  function_name = "${var.instance_alias}-chat-broker"
  role          = aws_iam_role.chat_broker.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.chat_broker.output_path
  source_code_hash = data.archive_file.chat_broker.output_base64sha256

  timeout     = 10
  memory_size = 256

  environment {
    variables = {
      CONNECT_INSTANCE_ID = aws_connect_instance.lab.id
      CONTACT_FLOW_ID     = aws_connect_contact_flow.main.contact_flow_id
      LOG_LEVEL           = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.chat_broker]
}
