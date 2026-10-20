# ---------------------------------------------------------------------------
# API Gateway -- the web entry point for chat.
#
# WHAT THIS IS NOT: this is not how Connect calls Lambda. Connect invokes
# a flow Lambda directly (see aws_connect_lambda_function_association in
# main.tf). API Gateway is not on the voice path at all.
#
# WHAT IT IS FOR: StartChatContact is a SigV4-signed IAM call. A browser
# cannot make it without holding AWS credentials, and a browser must never
# hold AWS credentials. So the shape is forced:
#
#   Browser -> API Gateway -> Lambda (holds the IAM role)
#                                |
#                                +-- calls StartChatContact
#                                +-- returns ONLY the ParticipantToken
#   Browser <------------------------+
#      |
#      +-> talks to the connectparticipant API with that token, no IAM
#
# The Lambda is a credential broker. API Gateway is the door in front of it,
# and throttling matters here because every call the broker serves creates a
# real contact occupying a real queue slot. Rate limiting this endpoint is
# queue-flood protection, not generic web hygiene.
#
# In production a WAF web ACL would sit in front of this, attached via
# CloudFront. WAF is omitted from this lab because a web ACL carries a
# standing monthly charge whether or not it sees traffic.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "chat" {
  name          = "${var.instance_alias}-chat-api"
  protocol_type = "HTTP"
  description   = "Credential broker for Amazon Connect web chat"

  cors_configuration {
    # Lock this to your actual web origin before putting it anywhere real.
    allow_origins = ["*"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["content-type"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "chat" {
  api_id                 = aws_apigatewayv2_api.chat.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.chat_broker.invoke_arn
  payload_format_version = "2.0"

  # Below the Lambda's own 10s timeout so the gateway does not give up first
  # and leave the function running against a dead connection.
  timeout_milliseconds = 15000
}

resource "aws_apigatewayv2_route" "chat" {
  api_id    = aws_apigatewayv2_api.chat.id
  route_key = "POST /chat"
  target    = "integrations/${aws_apigatewayv2_integration.chat.id}"
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.instance_alias}-chat-api"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "chat" {
  api_id      = aws_apigatewayv2_api.chat.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    # The control that matters. Without it, one script can fill the queue
    # and every real customer hears "we are experiencing high call volume".
    throttling_rate_limit  = 10 # steady-state requests per second
    throttling_burst_limit = 20 # burst allowance
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn

    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

# API Gateway must be granted permission to invoke the function. This is a
# resource-based policy on the Lambda, the same mechanism the Connect
# association uses -- different principal, same idea.
resource "aws_lambda_permission" "api_invoke_chat" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.chat_broker.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.chat.execution_arn}/*/*"
}
