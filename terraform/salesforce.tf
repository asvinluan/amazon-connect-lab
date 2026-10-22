# ---------------------------------------------------------------------------
# salesforce_lookup -- looks the caller up in a Salesforce Contact.
#
# A third Lambda, kept apart from caller_lookup on purpose: it is NOT wired
# into the contact flow, so the working voice path is untouched while the
# Salesforce integration is proven on its own.
#
# The credentials live in AWS Secrets Manager, not in this repository and not
# in Terraform state. Terraform only READS the secret's address (a data source
# fetches metadata, never the value) so the function's role can be scoped to
# exactly that one secret. Create the secret by hand first -- see the README.
# ---------------------------------------------------------------------------

data "archive_file" "salesforce_lookup" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/salesforce_lookup"
  output_path = "${path.module}/build/salesforce_lookup.zip"
}

data "aws_secretsmanager_secret" "salesforce" {
  name = var.salesforce_secret_name
}

resource "aws_cloudwatch_log_group" "salesforce_lookup" {
  name              = "/aws/lambda/${var.instance_alias}-salesforce-lookup"
  retention_in_days = 14
}

resource "aws_iam_role" "salesforce_lookup" {
  name = "${var.instance_alias}-salesforce-lookup"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "salesforce_lookup" {
  name = "salesforce-lookup-policy"
  role = aws_iam_role.salesforce_lookup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.salesforce_lookup.arn}:*"
      },
      {
        Sid    = "ReadSalesforceSecret"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        # One action on one secret. No secretsmanager:*, no Resource "*".
        Resource = data.aws_secretsmanager_secret.salesforce.arn
      },
    ]
  })
}

resource "aws_lambda_function" "salesforce_lookup" {
  function_name = "${var.instance_alias}-salesforce-lookup"
  role          = aws_iam_role.salesforce_lookup.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  filename         = data.archive_file.salesforce_lookup.output_path
  source_code_hash = data.archive_file.salesforce_lookup.output_base64sha256

  # The handler gives itself a 4-second budget; this is the hard backstop and
  # stays under Connect's 8-second ceiling (see the variable validation).
  timeout     = var.lambda_timeout_seconds
  memory_size = 256

  environment {
    variables = {
      SALESFORCE_SECRET_ID        = data.aws_secretsmanager_secret.salesforce.name
      SALESFORCE_CALL_TIMEOUT_SEC = "2"
      SALESFORCE_TOTAL_BUDGET_SEC = "4"
      LOG_LEVEL                   = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.salesforce_lookup]
}
