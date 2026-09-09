
resource "aws_lambda_function" "blog" {
  function_name    = "blog-tool-lambda"
  role             = var.lambda_exec
  filename         = var.blog_zip_path
  source_code_hash = filebase64sha256(var.blog_zip_path)
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = 15


  environment {
    variables = {
      GITHUB_OWNER  = var.github_owner
      GITHUB_REPO   = var.github_repo
      GITHUB_BRANCH = var.github_branch
      BLOG_PATH     = var.blog_path
      GITHUB_API = var.github_api
      JSDELIVR_BASE = var.jsdeliver_base
    }
  }
}

resource "aws_lambda_function" "projects" {
  function_name    = "projects-tool-lambda"
  role             = var.lambda_exec
  filename         = var.projects_zip_path
  source_code_hash = filebase64sha256(var.projects_zip_path)
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = 15


  environment {
    variables = {
      GITHUB_OWNER  = var.github_owner
      GITHUB_REPO   = var.github_repo
      GITHUB_BRANCH = var.github_branch
      PROJECTS_PATH = var.projects_path
      GITHUB_API = var.github_api
      JSDELIVR_BASE = var.jsdeliver_base
    }
  }
}

resource "aws_lambda_function" "resume" {
  function_name    = "resume-tool-lambda"
  role             = var.lambda_exec
  filename         = var.resume_zip_path
  source_code_hash = filebase64sha256(var.resume_zip_path)
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = 15


  environment {
    variables = {
      GITHUB_OWNER  = var.github_owner
      GITHUB_REPO   = var.github_repo
      GITHUB_BRANCH = var.github_branch
      RESUME_PATH   = var.resume_path
      GITHUB_API = var.github_api
      JSDELIVR_BASE = var.jsdeliver_base
    }
  }
}
