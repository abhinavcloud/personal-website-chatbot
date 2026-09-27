resource "aws_bedrockagentcore_agent_runtime" "agentcore_runtime" {
  agent_runtime_name = "agentcore_runtime"
  role_arn           = var.runtime_role

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.container_uri
    }
    }

  network_configuration {
    network_mode = "PUBLIC"
   }

  authorizer_configuration {
  custom_jwt_authorizer {
    discovery_url   = var.cognito_discovery_url
    allowed_clients = [var.cognito_app_client_id]
    }
  }

  request_header_configuration {
  request_header_allowlist = ["Authorization"]
  } 


  environment_variables = {
    MODEL_ID = var.model_id
    STEERING_MODEL_ID = var.steering_model_id
    REGION = var.region
    STEERING_REGION = var.steering_region
    GATEWAY_URL = var.gateway_url
    AGENTCORE_MEMORY_ID = var.memory_id
    COGNITO_ISSUER_URL    = var.cognito_issuer_url
    COGNITO_APP_CLIENT_ID = var.cognito_app_client_id
    COGNITO_USERINFO_URL = var.cognito_userinfo_url
    MAIL_TABLE_NAME = var.mail_table_name
  }

}

resource "aws_bedrockagentcore_agent_runtime_endpoint" "agentcore_runtime_endpoint" {
  name             = "agentcore_runtime_endpoint"
  agent_runtime_id = aws_bedrockagentcore_agent_runtime.agentcore_runtime.agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.agentcore_runtime.agent_runtime_version
  description      = "Endpoint for agent runtime communication"
}

