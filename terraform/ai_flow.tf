# ---------------------------------------------------------------------------
# lab-ai-flow -- the guarded AI answer, wired into a contact flow.
#
# A third flow beside lab-main-flow and lab-salesforce-flow. Neither of those is
# touched. The caller picks a canned question with a keypress (Lex replaces the
# keypad in the next sitting), the flow asks the ai_assist Lambda, and:
#
#   answered or blocked  ->  the caller hears the sentence, then goes to an agent
#   anything else        ->  the caller goes straight to an agent, no AI
#
# "Anything else" is the point of this build: shadow, off, error, a crash and a
# timeout all end at a human, never in silence.
#
# THE SWITCH. One Parameter Store value, /<alias>/ai/mode, decides what the
# Lambda does: off, shadow or live. Flip it with scripts/ai-flow/set-mode.ps1.
# Terraform creates the parameter as "off" and then never overwrites it
# (ignore_changes), so a later apply does not undo a switch someone flipped.
# ---------------------------------------------------------------------------

resource "aws_ssm_parameter" "ai_mode" {
  name        = "/${var.instance_alias}/ai/mode"
  description = "AI assist mode: off, shadow or live. test_fail, test_crash and test_slow break it on purpose."
  type        = "String"
  value       = "off"

  lifecycle {
    ignore_changes = [value]
  }
}

# The Lambda may read this one parameter and nothing else in Parameter Store.
resource "aws_iam_role_policy" "ai_assist_mode" {
  name = "ai-assist-mode-policy"
  role = aws_iam_role.ai_assist.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTheModeSwitch"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = aws_ssm_parameter.ai_mode.arn
    }]
  })
}

# Lets connect.amazonaws.com invoke the function, scoped to this instance.
# Without it the Invoke block fails at runtime with no obvious error.
resource "aws_connect_lambda_function_association" "ai_assist" {
  instance_id  = aws_connect_instance.lab.id
  function_arn = aws_lambda_function.ai_assist.arn
}

resource "aws_connect_contact_flow" "ai" {
  instance_id = aws_connect_instance.lab.id
  name        = "lab-ai-flow"
  description = "Inbound: pick a question, guarded AI answer with fallback to an agent"
  type        = "CONTACT_FLOW"

  content = templatefile("${path.module}/flows/ai.json.tftpl", {
    lambda_arn = aws_lambda_function.ai_assist.arn
    queue_arn  = aws_connect_queue.general.arn
  })

  depends_on = [aws_connect_lambda_function_association.ai_assist]
}

output "ai_flow_name" {
  description = "Point a claimed phone number at this flow to test the AI path."
  value       = aws_connect_contact_flow.ai.name
}

output "ai_mode_parameter" {
  description = "The Parameter Store name that holds the AI mode switch."
  value       = aws_ssm_parameter.ai_mode.name
}
