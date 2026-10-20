output "connect_instance_id" {
  description = "Connect instance id. Needed by the AWS CLI and by the flow audit script."
  value       = aws_connect_instance.lab.id
}

output "connect_admin_url" {
  description = "Admin site. Flows, queues, users and the CCP live here -- not in the AWS console."
  value       = "https://${var.instance_alias}.my.connect.aws"
}

output "contact_flow_id" {
  description = "Main inbound flow. Attach a claimed phone number to this."
  value       = aws_connect_contact_flow.main.contact_flow_id
}

output "queue_id" {
  value = aws_connect_queue.general.queue_id
}

output "routing_profile_id" {
  value = aws_connect_routing_profile.general.routing_profile_id
}

output "chat_api_endpoint" {
  description = "POST here to start a web chat. Returns a ParticipantToken, never AWS credentials."
  value       = "${aws_apigatewayv2_api.chat.api_endpoint}/chat"
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${aws_cloudwatch_dashboard.lab.dashboard_name}"
}

output "next_steps" {
  description = "What Terraform cannot do for you."
  value       = <<-EOT

    Terraform has built the instance, queue, routing profile, flow, Lambdas,
    API and alarms. Three things still need a human:

    1. Claim a phone number and attach it to the flow:
         aws connect associate-phone-number-contact-flow \
           --phone-number-id <id> \
           --instance-id ${aws_connect_instance.lab.id} \
           --contact-flow-id ${aws_connect_contact_flow.main.contact_flow_id}

       Australian numbers need identity verification through a support case,
       which takes 1-3 business days. A claimed number is billed monthly --
       release it when you are done.

    2. Create an agent in the admin site and assign the routing profile,
       then open the CCP and go Available. A call with no available agent
       sits in the queue and proves nothing.

    3. Confirm the SNS email subscription if you set alarm_email. It stays
       pending until you click the link.

    Admin site: https://${var.instance_alias}.my.connect.aws
  EOT
}
