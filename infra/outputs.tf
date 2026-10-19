output "sns_topic_arn" {
  description = "Topic the weekly report is published to."
  value       = aws_sns_topic.watchdog.arn
}

output "lambda_function_name" {
  description = "Use this with aws lambda invoke to trigger a report on demand."
  value       = aws_lambda_function.watchdog.function_name
}

output "log_group_name" {
  description = "CloudWatch log group for the Lambda."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "schedule_name" {
  description = "EventBridge Scheduler schedule that runs the Lambda weekly."
  value       = aws_scheduler_schedule.weekly.name
}

output "budget_name" {
  description = "AWS Budgets monthly cost budget."
  value       = aws_budgets_budget.monthly.name
}
