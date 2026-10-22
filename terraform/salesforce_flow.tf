# ---------------------------------------------------------------------------
# lab-salesforce-flow -- the Salesforce lookup, wired into a contact flow.
#
# A second flow beside lab-main-flow, not a change to it. lab-main-flow keeps
# using the DynamoDB lookup, so the working voice path is untouched. This flow
# invokes salesforce_lookup, copies the answer into contact attributes, greets
# the caller by name and queues them.
#
# The association is the hard requirement: it adds the resource-based policy
# that lets connect.amazonaws.com invoke the function, scoped to this instance.
# Without it the Invoke block fails at runtime with no obvious error.
# ---------------------------------------------------------------------------

resource "aws_connect_lambda_function_association" "salesforce_lookup" {
  instance_id  = aws_connect_instance.lab.id
  function_arn = aws_lambda_function.salesforce_lookup.arn
}

resource "aws_connect_contact_flow" "salesforce" {
  instance_id = aws_connect_instance.lab.id
  name        = "lab-salesforce-flow"
  description = "Inbound: look the caller up in Salesforce, greet by name, queue to an agent"
  type        = "CONTACT_FLOW"

  content = templatefile("${path.module}/flows/salesforce.json.tftpl", {
    lambda_arn = aws_lambda_function.salesforce_lookup.arn
    queue_arn  = aws_connect_queue.general.arn
  })

  depends_on = [aws_connect_lambda_function_association.salesforce_lookup]
}
