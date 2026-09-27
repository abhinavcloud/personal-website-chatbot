data "aws_region" "current" {}

data "aws_availability_zones" "az" { state = "available" }

data "aws_caller_identity" "current" {}

module "iam" {
  source          = "./iam/"
  project_name    = var.project_name
  lambda_resume   = module.lambda.resume_arn
  lambda_blog     = module.lambda.blog_arn
  lambda_projects = module.lambda.projects_arn
  region          = data.aws_region.current.region
  account_id      = data.aws_caller_identity.current.account_id
  ecr_arn         = var.ecr_arn
  gateway_arn     = module.agentcore-gateway.gateway_arn
  memory_arn      = module.agentcore-memory.memory_arn
  model_id        = var.model_id
  ses_arn         = module.ses.ses_arn
  recipient_email = var.recipient_email
  lambda_email    = module.lambda.email_arn
  mail_table_arn  = module.dynamodb.table_arn
  
}


module "lambda" {
  source            = "./lambda/"
  project_name      = var.project_name
  blog_zip_path     = var.blog_zip_path
  projects_zip_path = var.projects_zip_path
  resume_zip_path   = var.resume_zip_path
  github_owner      = var.github_owner
  github_repo       = var.github_repo
  github_branch     = var.github_branch
  resume_path       = var.resume_path
  blog_path         = var.blog_path
  projects_path     = var.projects_path
  lambda_exec       = module.iam.lambda_exec
  github_api        = var.github_api
  jsdeliver_base    = var.jsdeliver_base
  assistant_email   = var.assistant_email
  recipient_email   = var.recipient_email
  send_email_path   = var.send_email_path
  lambda_ses        = module.iam.lambda_ses_exec
  mail_table_name   = module.dynamodb.table_name
}


module "agentcore-gateway" {
  source          = "./agentcore-gateway"
  project_name    = var.project_name
  gateway_exec    = module.iam.gateway_exec
  lambda_resume   = module.lambda.resume_arn
  lambda_blog     = module.lambda.blog_arn
  lambda_projects = module.lambda.projects_arn
  lambda_email    = module.lambda.email_arn

}

module "agentcore-runtime" {
  source                = "./agentcore-runtime"
  runtime_role          = module.iam.runtime_role
  container_uri         = var.container_uri
  model_id              = var.model_id
  steering_model_id     = var.steering_model_id
  region                = data.aws_region.current.region
  steering_region       = var.steering_region
  gateway_url           = module.agentcore-gateway.gateway_url
  cognito_app_client_id = module.cognito.app_client_id
  cognito_discovery_url = module.cognito.discovery_url
  memory_id             = module.agentcore-memory.memory_id
  cognito_issuer_url    = module.cognito.issuer_url
  cognito_userinfo_url  = module.cognito.userinfo_url
  mail_table_name       = module.dynamodb.table_name
}

module "agentcore-memory" {
  source = "./agentcore-memory"
}

module "cognito" {
  source        = "./cognito"
  client_id     = var.client_id
  client_secret = var.client_secret
  root_domain   = var.root_domain
  app_name      = var.app_name


}

module "ses" {
  source          = "./ses"
  assistant_email = var.assistant_email
  website         = var.website
}

module "dynamodb" {
  source   = "./dynamodb"
  app_name = var.app_name
}