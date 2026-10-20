# ---------------------------------------------------------------------------
# Amazon Connect: instance, hours, queue, routing profile, contact flow.
#
# Build order is forced by dependency, and Terraform works it out from the
# references: hours -> queue -> routing profile -> flow. A queue cannot exist
# without hours of operation, and a routing profile cannot reference a queue
# that does not exist yet.
# ---------------------------------------------------------------------------

resource "aws_connect_instance" "lab" {
  # CONNECT_MANAGED stores users inside Connect. The alternative is SAML,
  # federated to an identity provider, which is what a bank uses.
  #
  # THIS FIELD IS IMMUTABLE. There is no API, console toggle or support case
  # that changes it after creation -- moving from CONNECT_MANAGED to SAML
  # means building a second instance and migrating everything into it.
  identity_management_type = "CONNECT_MANAGED"

  instance_alias         = var.instance_alias
  inbound_calls_enabled  = true
  outbound_calls_enabled = true

  # Off by default, and the single most useful thing to turn on. Without flow
  # logs you cannot see which block a call died in, and every failure becomes
  # guesswork.
  contact_flow_logs_enabled = true

  lifecycle {
    # identity_management_type and instance_alias are both immutable, so
    # Terraform's only way to honour an edit to either is destroy-then-create
    # -- which takes every queue, routing profile, flow and user with it.
    # This turns that into a plan-time error instead of an outage.
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Hours of operation
# ---------------------------------------------------------------------------

resource "aws_connect_hours_of_operation" "business" {
  instance_id = aws_connect_instance.lab.id
  name        = "lab-hours-business"
  description = "Mon-Fri 08:00-20:00 Melbourne time"

  # IANA name, not an offset. An offset like +10:00 is wrong for half the year
  # because it does not carry the daylight-saving transition.
  time_zone = "Australia/Melbourne"

  dynamic "config" {
    for_each = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

    content {
      day = config.value

      start_time {
        hours   = 8
        minutes = 0
      }

      end_time {
        hours   = 20
        minutes = 0
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

resource "aws_connect_queue" "general" {
  instance_id           = aws_connect_instance.lab.id
  name                  = "lab-queue-general"
  description           = "General enquiries"
  hours_of_operation_id = aws_connect_hours_of_operation.business.hours_of_operation_id

  # See the variable description: this is what makes the At capacity branch
  # reachable. Set low on purpose so the overflow path can be tested.
  max_contacts = var.queue_max_contacts
}

# ---------------------------------------------------------------------------
# Routing profile
#
# This single resource encodes the whole routing model, and it is the one
# worth being able to explain from memory.
# ---------------------------------------------------------------------------

resource "aws_connect_routing_profile" "general" {
  instance_id               = aws_connect_instance.lab.id
  name                      = "lab-rp-general"
  description               = "All-rounder profile for the lab"
  default_outbound_queue_id = aws_connect_queue.general.queue_id

  media_concurrencies {
    channel = "VOICE"
    # Voice is always 1. The API accepts other values and the service
    # ignores them. A channel absent from this list cannot be received
    # at all, which is the usual cause of "I enabled chat but agents
    # never get chats".
    concurrency = 1
  }

  queue_configs {
    channel  = "VOICE"
    queue_id = aws_connect_queue.general.queue_id

    # Lower number wins, range 1-99. When an agent frees up and several of
    # their queues have contacts waiting, Connect takes from the lowest
    # priority number; ties break on longest waiting.
    priority = 1

    # Seconds a contact must have waited before THIS profile considers it.
    # This is how overflow is built without any flow logic: give specialists
    # the queue at delay 0 and generalists the same queue at delay 30, and
    # the specialists get 30 seconds of exclusive first refusal.
    delay = 0
  }
}

# ---------------------------------------------------------------------------
# Contact flow
#
# The flow is JSON. Resource ARNs are not known until apply time, so the JSON
# is a template and templatefile() injects them. Hand-editing ARNs into flow
# JSON is how you get a flow that works in dev and fails in prod.
# ---------------------------------------------------------------------------

resource "aws_connect_contact_flow" "main" {
  instance_id = aws_connect_instance.lab.id
  name        = "lab-main-flow"
  description = "Inbound: greet, look up the caller, queue to an agent"
  type        = "CONTACT_FLOW"

  content = templatefile("${path.module}/flows/main.json.tftpl", {
    lambda_arn = aws_lambda_function.caller_lookup.arn
    queue_arn  = aws_connect_queue.general.arn
  })

  depends_on = [
    # The flow references the function, so the association must exist first
    # or the flow is created pointing at a function Connect may not invoke.
    aws_connect_lambda_function_association.caller_lookup,
  ]
}

# ---------------------------------------------------------------------------
# Lambda association
#
# This is a hard requirement, not a convenience. It adds a resource-based
# policy on the function allowing lambda:InvokeFunction from
# connect.amazonaws.com, scoped to this instance. Without it the flow's
# Invoke block fails at runtime.
# ---------------------------------------------------------------------------

resource "aws_connect_lambda_function_association" "caller_lookup" {
  instance_id  = aws_connect_instance.lab.id
  function_arn = aws_lambda_function.caller_lookup.arn
}
