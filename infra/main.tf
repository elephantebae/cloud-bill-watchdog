############################################################
# Lookups and shared values
############################################################

# Reads the account ID and partition at plan time so neither is hard coded.
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name       = "bill-watchdog"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Cross region inference profiles look like "us.anthropic.claude...". Calling
  # one needs permission on the profile itself and on the underlying foundation
  # model in every region the profile can route to.
  profile_match     = regex("^((?:us|eu|apac|us-gov|global|jp|au|ca)\\.)?(.+)$", var.model_id)
  is_profile        = local.profile_match[0] != null
  base_model_id     = local.profile_match[1]
  bedrock_model_arn = "arn:${local.partition}:bedrock:*::foundation-model/${local.base_model_id}"
  bedrock_profile_arn = (
    local.is_profile
    ? "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${var.model_id}"
    : null
  )
}

############################################################
# 1. SNS topic and email subscription
############################################################

# SNS (Simple Notification Service) is a pub/sub service. The Lambda publishes
# one message to this topic and SNS fans it out to every subscriber.
resource "aws_sns_topic" "watchdog" {
  name = "BillWatchdog"
}

# Email subscriptions start as "PendingConfirmation". AWS sends a confirmation
# link and nothing is delivered until you click it.
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.watchdog.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

############################################################
# 2. IAM role for the Lambda
############################################################

# Trust policy: says WHO may assume the role. Only the Lambda service can.
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# AWS managed policy that lets the function write its own CloudWatch logs.
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Permissions policy: says WHAT the role may do. Least privilege means only the
# three actions the code calls, scoped as tightly as each service allows.
data "aws_iam_policy_document" "lambda_permissions" {
  # Cost Explorer has no resource level permissions, so "*" is the only option.
  statement {
    sid       = "ReadCosts"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }

  # Publish to this one topic only, not every topic in the account.
  statement {
    sid       = "PublishReport"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.watchdog.arn]
  }

  # Invoke only the configured model (and its inference profile, if used).
  # The Converse API used in the code is authorized by bedrock:InvokeModel.
  statement {
    sid       = "InvokeSummaryModel"
    actions   = ["bedrock:InvokeModel"]
    resources = compact([local.bedrock_model_arn, local.bedrock_profile_arn])
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-least-privilege"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

############################################################
# 3. Lambda function
############################################################

# Zips src/ on your machine at plan time. The hash below changes whenever the
# code changes, which tells Terraform to redeploy.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/lambda.zip"
  excludes    = ["__pycache__", "**/__pycache__/**", "**/*.pyc"]
}

# Lambda runs code without servers. You pay per request and per GB second, and
# a weekly 60 second run sits well inside the free tier.
resource "aws_lambda_function" "watchdog" {
  function_name    = local.name
  description      = "Weekly AWS spend report summarized by Bedrock and sent via SNS"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  architectures    = ["arm64"] # Graviton is about 20% cheaper per GB second
  memory_size      = 128
  timeout          = 60
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      TOPIC_ARN    = aws_sns_topic.watchdog.arn
      MODEL_ID     = var.model_id
      WEEKLY_LIMIT = tostring(var.weekly_limit)
    }
  }

  # Create the log group first so Lambda doesn't auto create one with
  # infinite retention.
  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
  ]
}

############################################################
# 4. CloudWatch log group
############################################################

# Lambda writes to /aws/lambda/<function name> by convention. Managing the group
# here lets us cap retention at 14 days so old logs don't accrue storage cost.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 14
}

############################################################
# 5. EventBridge Scheduler
############################################################

# EventBridge Scheduler assumes this role when it fires. It is a separate role
# from the Lambda's because each principal should hold only what it needs.
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Blocks the "confused deputy" problem: only schedules in this account can
    # use the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler_permissions" {
  statement {
    sid       = "InvokeWatchdogOnly"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.watchdog.arn, "${aws_lambda_function.watchdog.arn}:*"]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-invoke-lambda"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_permissions.json
}

# Scheduler is the newer, timezone aware alternative to EventBridge rules. With
# a named timezone it follows daylight saving, so 8:00am stays 8:00am local.
# Cron fields: minute hour day_of_month month day_of_week year.
resource "aws_scheduler_schedule" "weekly" {
  name                         = "${local.name}-weekly"
  description                  = "Runs the Bill Watchdog every Monday at 8:00am Pacific"
  schedule_expression          = "cron(0 8 ? * MON *)"
  schedule_expression_timezone = "America/Los_Angeles"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.watchdog.arn
    role_arn = aws_iam_role.scheduler.arn

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

############################################################
# 6. AWS Budgets
############################################################

# Budgets is the safety net that does not depend on any of the code above. It
# emails you straight from AWS when spend crosses a threshold. Budgets that
# only send alerts are free; only budgets with automated actions are billed
# after the first two.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.monthly_budget)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert when real, already incurred spend passes 80% of the budget.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # Alert early when AWS projects the month will end above 100%.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
