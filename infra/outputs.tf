output "agentcore_runtime_endpoint" {
  value = module.agentcore-runtime.agentcore_runtime_endpoint
}

output "agentcore_runtime" {
  value = module.agentcore-runtime.agentcore_runtime
}

# output "cognito_app_client_id" {
#  value = module.cognito.app_client_id
# }


# output "ses_domain_verification_token" {
#    value = module.ses.ses_domain_verification_token
# }

# output "ses_dkim_tokens" {
#  value = module.ses.ses_dkim_tokens
#}

# output "mail_sender_identity_arn" {
#  description = "SES sender identity ARN for the mail Lambda IAM policy."
#  value       = module.ses.mail_sender_identity_arn
#}
