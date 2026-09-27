resource "aws_bedrockagentcore_gateway" "gateway" {
  name     = "${var.project_name}-gateway"
  role_arn = var.gateway_exec

  protocol_type = "MCP"

  authorizer_type = "AWS_IAM"
  #authorizer_configuration {
  #  custom_jwt_authorizer {
  #    discovery_url = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.gateway_auth.id}/.well-known/openid-configuration"
  #    allowed_clients = [aws_cognito_user_pool_client.gateway_client.id]
  #  }
  #}
}

# --- Target 1: Resume Lambda -------------------------------------------

resource "aws_bedrockagentcore_gateway_target" "resume" {
  name               = "ResumeTarget"
  gateway_identifier = aws_bedrockagentcore_gateway.gateway.gateway_id
  description        = "Resume contact info + full resume body"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_resume

        tool_schema {
          dynamic "inline_payload" {
            for_each = local.resume_tools
            content {
              name        = inline_payload.key
              description = inline_payload.value.description

              input_schema {
                type = "object"

                dynamic "property" {
                  for_each = inline_payload.value.properties
                  content {
                    name     = property.key
                    type     = property.value.type
                    required = property.value.required
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# --- Target 2: Blog Lambda ----------------------------------------------

resource "aws_bedrockagentcore_gateway_target" "blog" {
  name               = "BlogTarget"
  gateway_identifier = aws_bedrockagentcore_gateway.gateway.gateway_id
  description        = "Blog listing + full post content"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_blog

        tool_schema {
          dynamic "inline_payload" {
            for_each = local.blog_tools
            content {
              name        = inline_payload.key
              description = inline_payload.value.description

              input_schema {
                type = "object"

                dynamic "property" {
                  for_each = inline_payload.value.properties
                  content {
                    name     = property.key
                    type     = property.value.type
                    required = property.value.required
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# --- Target 3: Projects Lambda -------------------------------------------

resource "aws_bedrockagentcore_gateway_target" "projects" {
  name               = "ProjectsTarget"
  gateway_identifier = aws_bedrockagentcore_gateway.gateway.gateway_id
  description        = "Project listing + full project content"

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_projects

        tool_schema {
          dynamic "inline_payload" {
            for_each = local.projects_tools
            content {
              name        = inline_payload.key
              description = inline_payload.value.description

              input_schema {
                type = "object"

                dynamic "property" {
                  for_each = inline_payload.value.properties
                  content {
                    name     = property.key
                    type     = property.value.type
                    required = property.value.required
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# --- Target 4: Email Lambda -------------------------------------------
resource "aws_bedrockagentcore_gateway_target" "email" {
  name               = "EmailTarget"
  gateway_identifier = aws_bedrockagentcore_gateway.gateway.gateway_id
  description        = "Send a message to Abhinav's fixed email address."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.lambda_email

        tool_schema {
          inline_payload {
            name        = "send_email"
            description = "Send email to Abhinav. The application supplies the authenticated user's reply-to address."

            input_schema {
              type = "object"

              property {
                name        = "subject"
                type        = "string"
                description = "Email subject."
                required    = true
              }

              property {
                name        = "body"
                type        = "string"
                description = "Plain-text email body."
                required    = true
              }

              property {
                name        = "reply_to"
                type        = "string"
                description = "Verified Cognito email supplied by trusted application code."
                required    = true
              }

              property {
                name        = "request_id"
                type        = "string"
                description = "Stable saved-email identifier supplied by backend code."
                required    = true
              }


            }
          }
        }
      }
    }
  }
}