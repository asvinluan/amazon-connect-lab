# ---------------------------------------------------------------------------
# Customer reference data.
#
# PAY_PER_REQUEST because a lab has no steady traffic and provisioned capacity
# on an idle table is pure waste. A production contact centre with a known
# busy-hour profile is the case for provisioned capacity plus autoscaling.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "customers" {
  name         = "${var.instance_alias}-customers"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "phoneNumber"

  attribute {
    name = "phoneNumber"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false # lab only; enable for anything holding real records
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# Seed data.
#
# Fabricated numbers in the Australian fictional-number range (+61 4XX XXX XXX
# reserved blocks used for drama and documentation). No real customer data is
# in this repository, and none should ever be.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table_item" "seed" {
  for_each = {
    "+61455500001" = { name = "Test Caller One", segment = "standard" }
    "+61455500002" = { name = "Test Caller Two", segment = "priority" }
  }

  table_name = aws_dynamodb_table.customers.name
  hash_key   = aws_dynamodb_table.customers.hash_key

  item = jsonencode({
    phoneNumber = { S = each.key }
    callerName  = { S = each.value.name }
    segment     = { S = each.value.segment }
  })
}
