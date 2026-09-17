output "blog_arn" {
    value = aws_lambda_function.blog.arn
}

output "projects_arn" {
    value = aws_lambda_function.projects.arn
}

output "resume_arn" {
    value = aws_lambda_function.resume.arn
}