output "agentcore_runtime_endpoint" {
    value = aws_bedrockagentcore_agent_runtime_endpoint.agentcore_runtime_endpoint.agent_runtime_endpoint_arn
}

output "agentcore_runtime" {
    value = aws_bedrockagentcore_agent_runtime_endpoint.agentcore_runtime_endpoint.agent_runtime_arn
}