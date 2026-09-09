data "aws_region" "current" {}

data "aws_availability_zones" "az" { state = "available" }

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

}

module "iam" {
  source       = "./iam/"
  project_name = var.project_name
}

