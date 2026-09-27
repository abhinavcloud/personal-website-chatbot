# ---Lambda Execution Role ---

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-tools-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Gateway Execurtion Role to Invoike the Lambda Targets

data "aws_iam_policy_document" "gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${var.account_id}:gateway/*"]
    }
  }
}

resource "aws_iam_role" "gateway_exec" {
  name               = "${var.project_name}-gateway-exec"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
}

data "aws_iam_policy_document" "gateway_invoke_lambdas" {
  statement {
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      var.lambda_blog,
      var.lambda_projects,
      var.lambda_resume,
      var.lambda_email,
    ]
  }
}

resource "aws_iam_role_policy" "gateway_invoke_lambdas" {
  name   = "invoke-tool-lambdas"
  role   = aws_iam_role.gateway_exec.id
  policy = data.aws_iam_policy_document.gateway_invoke_lambdas.json

}


# --- Agentcore Runtime to get ECR Image and build runtime container

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ecr_permissions" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    effect    = "Allow"
    resources = ["*"]
  }

  statement {
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer"
    ]
    effect    = "Allow"
    resources = [var.ecr_arn]
  }
}

resource "aws_iam_role" "bedrock_agent_runtime_role" {
  name               = "bedrock-agentcore-runtime-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

resource "aws_iam_role_policy" "bedrock_agent_runtime_role_policy" {
  role   = aws_iam_role.bedrock_agent_runtime_role.id
  policy = data.aws_iam_policy_document.ecr_permissions.json
}

# --- Runtime Execution Policy to Invoke Gateway

data "aws_iam_policy_document" "gateway_invocation" {
  statement {
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeGateway"
    ]
    resources = [var.gateway_arn]

  }
}

resource "aws_iam_role_policy" "bedrock_agent_runtime_gateway_execution_role_policy" {
  role   = aws_iam_role.bedrock_agent_runtime_role.id
  policy = data.aws_iam_policy_document.gateway_invocation.json
}

# --- Runtime Excution Policy to Invoke Model
#data "aws_iam_policy_document" "model_invocation" {
#  statement {
#    effect = "Allow"
#    actions = [
#      "bedrock:InvokeModelWithResponseStream"
#    ]
#    resources = [var.model_id]
#  }
#}

#resource "aws_iam_role_policy" "bedrock_agent_runtime_model_execution_role_policy" {
#  role   = aws_iam_role.bedrock_agent_runtime_role.id
#  policy = data.aws_iam_policy_document.model_invocation.json
#}



# Agentcore runtime execution policy to invoke model
resource "aws_iam_role_policy_attachment" "agentcore_model_role_policy_attachment" {
  role       = aws_iam_role.bedrock_agent_runtime_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

# --- Runtime Execution Policy to invoke Agentcore Memory
data "aws_iam_policy_document" "list_memory_events" {
  statement {
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:ListMemoryRecords",
    ]
    resources = [var.memory_arn]

  }
} 


resource "aws_iam_role_policy" "bedrock_agent_runtime_list_memory_events_role_policy" {
  role   = aws_iam_role.bedrock_agent_runtime_role.id
  policy = data.aws_iam_policy_document.list_memory_events.json
}

# --- Runtime Cloudwatch and XRAY Logging
data "aws_iam_policy_document" "runtime_logs_xray" {
  statement {
    sid    = "CloudWatchLogsAccess"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/bedrock-agentcore/*",
    ]
  }

  statement {
    sid       = "XRayTracing"
    effect    = "Allow"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "bedrock_agent_runtime_logs_xray_policy" {
  role   = aws_iam_role.bedrock_agent_runtime_role.id
  policy = data.aws_iam_policy_document.runtime_logs_xray.json
}

# Mail Lambda SES Execution Role and Policies

resource "aws_iam_role" "lambda_ses_exec" {
  name               = "lambda-ses-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_ses_basic_logs" {
  role       = aws_iam_role.lambda_ses_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_ses_invocation" {
  statement {
    effect = "Allow"
    actions = [
      "ses:SendEmail"
    ]
    resources = [
      var.ses_arn,
      "arn:aws:ses:${var.region}:${var.account_id}:identity/${var.recipient_email}",
        ]

  }
}

resource "aws_iam_role_policy" "lambda_ses_execution_role_policy" {
  role   = aws_iam_role.lambda_ses_exec.id
  policy = data.aws_iam_policy_document.lambda_ses_invocation.json
}

# Agentcore Runtime Dynamo DB Execution role

resource "aws_iam_role_policy" "runtime_mail_storage" {
  role = aws_iam_role.bedrock_agent_runtime_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
      Resource = var.mail_table_arn
    }]
  })
}

# Mail Lambda Dynamo DB Execution role

resource "aws_iam_role_policy" "lambda_mail_storage" {
  role = aws_iam_role.lambda_ses_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
      ]
      Resource = var.mail_table_arn
    }]
  })
}