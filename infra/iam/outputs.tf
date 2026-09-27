output "lambda_exec" {
    value = aws_iam_role.lambda_exec.arn
}

output "gateway_exec" {
    value = aws_iam_role.gateway_exec.arn
}

output "runtime_role" {
    value = aws_iam_role.bedrock_agent_runtime_role.arn
}

output "lambda_ses_exec" {
    value = aws_iam_role.lambda_ses_exec.arn
}
