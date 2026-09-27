output "ses_arn" {
  description = "SES sender identity ARN for the mail Lambda IAM policy."
  value       = aws_ses_email_identity.mail.arn
}

output "ses_domain_verification_token" {
  value = aws_ses_domain_identity.website.verification_token
}

output "ses_dkim_tokens" {
  value = aws_ses_domain_dkim.website.dkim_tokens
}