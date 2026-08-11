output "inference_endpoint_arn" {
  value       = aws_bedrock_inference_profile.bedrock_inference_profile_personal_wesbite.arn
  description = "The ARN of the Bedrock Inference Profile"
}

output "guardrail_id" {
  value       = aws_bedrock_guardrail.bedrock_guardrail_personal_website.guardrail_id
  description = "The ID of the Bedrock Guardrail"
}

output "gurardrail_version_number" {
  value       = aws_bedrock_guardrail.bedrock_guardrail_personal_website.version
  description = "The version number of the Bedrock Guardrail"
}

output "knowlege_base_id" {
  value = aws_bedrockagent_knowledge_base.kb_personal_website_1.id
}