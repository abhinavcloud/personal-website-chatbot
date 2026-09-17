output "lambda_exec" {
    value = aws_iam_role.lambda_exec.arn
}

output "gateway_exec" {
    value = aws_iam_role.gateway_exec.arn
}