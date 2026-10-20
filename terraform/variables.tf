variable "region" {
  description = "AWS region. Amazon Connect is not available in every region; ap-southeast-2 (Sydney) is the closest to Melbourne."
  type        = string
  default     = "ap-southeast-2"
}

variable "instance_alias" {
  description = "Connect instance alias. Globally unique within the region, and becomes the subdomain of the access URL."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,44}[a-z0-9]$", var.instance_alias))
    error_message = "Alias must be lowercase alphanumeric with hyphens, 3-46 characters, not starting or ending with a hyphen."
  }
}

variable "queue_max_contacts" {
  description = "Maximum contacts allowed in the queue. This is what drives the 'At capacity' branch of Transfer to queue -- omit it and the queue is unbounded, the branch never fires, and the overflow path cannot be tested."
  type        = number
  default     = 5
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Must stay under Connect's own 8-second invocation limit: Connect abandons the invoke at 8s and takes the Error branch regardless of what the function's timeout says."
  type        = number
  default     = 5

  validation {
    condition     = var.lambda_timeout_seconds < 8
    error_message = "Must be under 8 seconds. Connect abandons a flow Lambda invocation at 8 seconds and takes the Error branch; a longer timeout cannot help and only delays the error."
  }
}

variable "lambda_duration_alarm_ms" {
  description = "Alarm threshold on Lambda p99 duration. Set below the Connect 8s ceiling so the alarm fires before callers start hitting the Error branch."
  type        = number
  default     = 6000
}

variable "alarm_email" {
  description = "Email address subscribed to the alarm SNS topic. Empty creates the topic with no subscription."
  type        = string
  default     = ""
}

variable "recording_retention_days" {
  description = "Not used by this lab (no recordings are configured) but kept to show the dev/prod divergence pattern: a lab keeps 30 days, a regulated production estate keeps 2555 (seven years)."
  type        = number
  default     = 30
}
