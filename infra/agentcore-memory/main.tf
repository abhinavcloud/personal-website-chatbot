resource "aws_bedrockagentcore_memory" "memory" {
  name                  = "agent_memory"
  event_expiry_duration = 30
}

resource "aws_bedrockagentcore_memory_strategy" "semantic" {
  name                = "semantic_strategy"
  memory_id           = aws_bedrockagentcore_memory.memory.id
  type                = "SEMANTIC"
  description         = "Semantic understanding strategy"
  namespace_templates = ["/users/{actorId}/facts/"]
}

resource "aws_bedrockagentcore_memory_strategy" "summary" {
  name                = "summary_strategy"
  memory_id           = aws_bedrockagentcore_memory.memory.id
  type                = "SUMMARIZATION"
  description         = "Text summarization strategy"
  namespace_templates = ["/users/{actorId}/sessions/{sessionId}/summaries/"]
}
