variable "aws_region" {
  description = "Region for the Lambda, SNS topic, and schedule. Must be a region where your Bedrock model is enabled."
  type        = string
  default     = "us-west-2"
}

variable "aws_profile" {
  description = "Name of the AWS CLI profile Terraform uses for credentials."
  type        = string
  default     = "default"
}

variable "alert_email" {
  description = "Address that receives the weekly report and budget alerts. Keep it in terraform.tfvars, which is gitignored."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "model_id" {
  description = "Bedrock model ID or inference profile ID, for example one that starts with us.anthropic.claude"
  type        = string

  validation {
    condition     = length(trimspace(var.model_id)) > 0
    error_message = "model_id must not be empty."
  }
}

variable "weekly_limit" {
  description = "Weekly spend in USD above which the report is flagged OVER LIMIT."
  type        = number
  default     = 5
}

variable "monthly_budget" {
  description = "Monthly AWS Budgets limit in USD."
  type        = number
  default     = 10
}
