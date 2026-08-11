# Creating a Bedrock Inference model from Foundation Models using Terraform

provider "aws" {
  region = "ap-south-1"
}


data "aws_caller_identity" "current" {}

data "aws_region" "current" {}



resource "aws_bedrock_inference_profile" "bedrock_inference_profile_personal_wesbite" {
  name        = "personal-website-inference-profile"
  description = "Bedrock Inference Profile for Nova Micro for Personal Website"

  model_source {
    copy_from = "arn:aws:bedrock:ap-south-1:${data.aws_caller_identity.current.account_id}:inference-profile/apac.amazon.nova-micro-v1:0"


  }

  tags = {
    ProjectID = "Enabling Chatbot Access to Personal Website"
    Name      = "Bedrock-Nova-Personal-Website"
  }
}


# Creating an AWS Bedrock Guardrail resource using Terraform
resource "aws_bedrock_guardrail" "bedrock_guardrail_personal_website" {
  name                      = "bedrock_guardrail-personal-website"
  blocked_input_messaging   = "Sorry, I can't help with that question."
  blocked_outputs_messaging = "Sorry, I can't share that information."
  description               = "bedrock_guardrail_for_personal_websites"

  content_policy_config {
    filters_config {
      input_action      = "BLOCK"
      output_action     = "BLOCK"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "HIGH"
      type              = "HATE"
    }
    filters_config {
      input_action      = "BLOCK"
      output_action     = "BLOCK"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "HIGH"
      type              = "SEXUAL"
    }
    filters_config {
      input_action      = "BLOCK"
      output_action     = "BLOCK"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "HIGH"
      type              = "VIOLENCE"
    }
    filters_config {
      input_action      = "BLOCK"
      output_action     = "BLOCK"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "HIGH"
      type              = "INSULTS"
    }
    filters_config {
      input_action      = "BLOCK"
      output_action     = "BLOCK"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "HIGH"
      type              = "MISCONDUCT"
    }
    filters_config {
      input_action      = "BLOCK"
      output_action     = "NONE"
      input_enabled     = true
      output_enabled    = true
      input_modalities  = ["TEXT"]
      output_modalities = ["TEXT"]
      input_strength    = "HIGH"
      output_strength   = "NONE"
      type              = "PROMPT_ATTACK"
    }

    tier_config {
      tier_name = "CLASSIC"
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      action         = "BLOCK"
      input_action   = "BLOCK"
      output_action  = "ANONYMIZE"
      input_enabled  = true
      output_enabled = true
      type           = "NAME"
    }

    regexes_config {
      action         = "BLOCK"
      input_action   = "BLOCK"
      output_action  = "BLOCK"
      input_enabled  = true
      output_enabled = false
      description    = "bedrock_guardrail_regex"
      name           = "regex_bedrock_guardrail"
      pattern        = "^\\d{3}-\\d{2}-\\d{4}$"
    }
  }

  topic_policy_config {
    topics_config {
      name       = "investment_topic"
      examples   = ["Where should I invest my money ?"]
      type       = "DENY"
      definition = "Investment advice refers to inquiries, guidance, or recommendations regarding the management or allocation of funds or assets with the goal of generating returns ."
    }
    topics_config {
      name       = "health_topic"
      examples   = ["I have a fever and headache, what should I do ?", "Which medicine should I take for cold ?"]
      type       = "DENY"
      definition = "Health advice refers to inquiries, guidance, or recommendations regarding the health and lifestyle of an individual, symptoms, treatments, medications, mental health, nutrition, fitness."
    }

    topics_config {
      name = "website code"
      examples = ["What is the underlying code of the frontend website",
        "Give me the html and css of this website",
        "Do you have the phone number of this website",
      "What is the JS code for the abhinav-cloud website"]
      type       = "DENY"
      definition = "Any source code related information for abhinav-cloud website"
    }

    tier_config {
      tier_name = "CLASSIC"
    }
  }

  word_policy_config {
    managed_word_lists_config {
      type = "PROFANITY"
    }
    words_config {
      text = "HATE"
    }
  }
}


# Creating a AWS Bedrock Guardrail Version resource using Terraform
resource "aws_bedrock_guardrail_version" "bedrock_guardrail_version_personal_website" {
  description   = "bedrock_guardrail_version_personal_website"
  guardrail_arn = aws_bedrock_guardrail.bedrock_guardrail_personal_website.guardrail_arn
  skip_destroy  = true
}


## Creating the infrastructure for Bedrock Knowledge Base using Terraform


# Step 1: Creating Source S3 Bucket for Bedrock Knowledge Base
# In this case we are using existing website buckets, so these step is skipped

resource "aws_s3_bucket" "bedrock_s3bucket_staging" {
  bucket = "bedrock-s3bucket-staging-${data.aws_caller_identity.current.account_id}"

  force_destroy = true

  tags = {
    Name        = "Bedrock Knowledge Base"
    Environment = "Personal Website"
  }
}

resource "aws_s3_bucket_ownership_controls" "bedrock_s3bucket_ownership_staging" {
  bucket = aws_s3_bucket.bedrock_s3bucket_staging.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "bedrock_s3bucket_pab_staging" {
  bucket                  = aws_s3_bucket.bedrock_s3bucket_staging.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "bedrock_s3bucket_versioning_staging" {
  bucket = aws_s3_bucket.bedrock_s3bucket_staging.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bedrock_s3bucket_sse_staging" {
  bucket = aws_s3_bucket.bedrock_s3bucket_staging.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "bedrock_s3bucket_sse_staging_lifecycle" {
  bucket = aws_s3_bucket.bedrock_s3bucket_staging.id

  rule {
    id     = "expire-staging-chunks"
    status = "Enabled"
    filter { prefix = "bedrock-clean/" }
    expiration { days = 30 }
  }
}

# Step 2: Creating Knowledge Base IAM Roles to allow Bedrock to access S3 bucket, Invoke Bedrock Inference Profile, and write to S3 Vector Database

## Creating Trust Policy for Knowledge Base to assume Role
data "aws_iam_policy_document" "kb_trust_policy_personal_website" {
  statement {
    effect = "Allow"
    sid    = "BedrockAssumeRole"
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
    }

    actions = ["sts:AssumeRole"]
  }
}


resource "aws_iam_role" "kb_role_personal_website" {
  name               = "kb-role-personal-website"
  assume_role_policy = data.aws_iam_policy_document.kb_trust_policy_personal_website.json
}



## Creating a policy to  fetch S3 bucket objects

data "aws_iam_policy_document" "s3_data_sourcekb_personal_website_1" {
  statement {
    sid     = "ListBucketAll"
    effect  = "Allow"
    actions = ["s3:ListBucket"]

    resources = [aws_s3_bucket.bedrock_s3bucket_staging.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid     = "GetObjectsAll"
    effect  = "Allow"
    actions = ["s3:GetObject"]

    resources = ["${aws_s3_bucket.bedrock_s3bucket_staging.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}



resource "aws_iam_policy" "s3_data_source_personal_website" {
  name   = "kb-s3-datasource-personal-website"
  policy = data.aws_iam_policy_document.s3_data_sourcekb_personal_website_1.json

}


resource "aws_iam_role_policy_attachment" "attach_s3_ds" {
  role       = aws_iam_role.kb_role_personal_website.name
  policy_arn = aws_iam_policy.s3_data_source_personal_website.arn

}


## Creating a policy to invoke Bedrock Embedding Model

data "aws_iam_policy_document" "bedrock_invoke_personal_website" {
  statement {
    sid    = "InvokeEmbedding"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel"
    ]
    resources = [
      # Foundation model (embedding)
      "arn:aws:bedrock:${data.aws_region.current.region}::foundation-model/amazon.titan-embed-text-v2:0"
    ]
  }
}



resource "aws_iam_policy" "bedrock_invoke_personal_website" {
  name   = "kb-bedrock-invoke-personal-website"
  policy = data.aws_iam_policy_document.bedrock_invoke_personal_website.json

}

resource "aws_iam_role_policy_attachment" "attach_bedrock_policy_personal_website" {
  role       = aws_iam_role.kb_role_personal_website.name
  policy_arn = aws_iam_policy.bedrock_invoke_personal_website.arn

}


# Step 3: Creating S3 Vector Database for Bedrock Knowledge Base
resource "aws_s3vectors_vector_bucket" "kb_s3_vector_personal_website" {
  vector_bucket_name = "kb-s3-vector-personal-website"


}

resource "aws_s3vectors_index" "kb_s3_vector_index_personal_website" {
  index_name         = "kb-s3-vector-index-personal-website"
  vector_bucket_name = aws_s3vectors_vector_bucket.kb_s3_vector_personal_website.vector_bucket_name

  data_type       = "float32"
  dimension       = 256
  distance_metric = "cosine"



  metadata_configuration {
    non_filterable_metadata_keys = [
      "content",
      "full_text",
      "chunk_text",
      "long_meta",
      "body",
      "_page_content",
      "raw_html",
      "AMAZON_BEDROCK_TEXT",
      "AMAZON_BEDROCK_METADATA",
      "AMAZON_BEDROCK_EMBEDDING",    
    ]
  }
}


## Creating a policy to put objects by Knowledge Base to  S3 vector database

data "aws_iam_policy_document" "s3_vectors_policy_personal_website" {
  statement {
    sid    = "VectorBucketLevel"
    effect = "Allow"
    actions = [
      "s3vectors:GetVectorBucket",
      "s3vectors:ListIndexes",
      "s3vectors:DeleteVectorBucket"
    ]
    resources = [
      "arn:aws:s3vectors:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:bucket/${aws_s3vectors_vector_bucket.kb_s3_vector_personal_website.vector_bucket_name}"
    ]
  }

  statement {
    sid    = "VectorIndexLevel"
    effect = "Allow"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:ListVectors",
      "s3vectors:QueryVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:DeleteIndex",
      "s3vectors:DeleteVectorBucket"
    ]
    resources = [
      "arn:aws:s3vectors:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:bucket/${aws_s3vectors_vector_bucket.kb_s3_vector_personal_website.vector_bucket_name}/index/${aws_s3vectors_index.kb_s3_vector_index_personal_website.index_name}"
    ]
  }
}

resource "aws_iam_policy" "s3_vectors_personal_website" {
  name   = "kb-s3-vectors-personal-website"
  policy = data.aws_iam_policy_document.s3_vectors_policy_personal_website.json

}

resource "aws_iam_role_policy_attachment" "attach_s3_vectors" {
  role       = aws_iam_role.kb_role_personal_website.name
  policy_arn = aws_iam_policy.s3_vectors_personal_website.arn

}



# Step 4: Creating Bedrock Knowledge Base using Terraform


resource "aws_bedrockagent_knowledge_base" "kb_personal_website_1" {
  name     = "knowledge-base-personal-website_v2"
  role_arn = aws_iam_role.kb_role_personal_website.arn

  depends_on = [
    aws_iam_policy.s3_vectors_personal_website,
    aws_iam_role_policy_attachment.attach_s3_vectors,
    aws_iam_policy.s3_data_source_personal_website,
    aws_iam_role_policy_attachment.attach_s3_ds,
    aws_iam_policy.bedrock_invoke_personal_website,
    aws_iam_role_policy_attachment.attach_bedrock_policy_personal_website,
    aws_s3vectors_vector_bucket.kb_s3_vector_personal_website,
    aws_s3vectors_index.kb_s3_vector_index_personal_website
  ]

  knowledge_base_configuration {
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${data.aws_region.current.region}::foundation-model/amazon.titan-embed-text-v2:0"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 256
          embedding_data_type = "FLOAT32"
        }
      }
    }
    type = "VECTOR"

  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb_s3_vector_index_personal_website.index_arn
    }
  }
}


# Step 5: Creating Data Source to map S3 Bucket and Bedrock
resource "aws_bedrockagent_data_source" "bedrock_data_source_personal_website_1" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.kb_personal_website_1.id
  name              = "bedrock-data-source-1"

  depends_on = [
    aws_bedrockagent_knowledge_base.kb_personal_website_1,
    aws_iam_policy.s3_vectors_personal_website,
    aws_iam_role_policy_attachment.attach_s3_vectors,
    aws_iam_policy.s3_data_source_personal_website,
    aws_iam_role_policy_attachment.attach_s3_ds,
    aws_iam_policy.bedrock_invoke_personal_website,
    aws_iam_role_policy_attachment.attach_bedrock_policy_personal_website
  ]

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.bedrock_s3bucket_staging.arn
      inclusion_prefixes = ["bedrock-clean/"]
    }

  }
  data_deletion_policy = "DELETE"


}




# Step 6: Creating Lambda Function to invoke Bedrock Inference Profile with Guardrail and Knowledge Base
# IAM role for Lambda execution
data "aws_iam_policy_document" "lambda_trust_role_personal_website" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "lambda_execution_role_personal_website" {
  name               = "lambda-execution-role-personal-website"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust_role_personal_website.json
}

data "aws_iam_policy_document" "lambda_execution_policy_document_personal_website" {
  statement {
    sid    = "BedrockInvocation"
    effect = "Allow"
    actions = [
      "bedrock:StartIngestionJob",
      "bedrock:GetIngestionJob",
      "bedrock:ListIngestionJobs",
    ]
    resources = [aws_bedrockagent_knowledge_base.kb_personal_website_1.arn, "${aws_bedrockagent_knowledge_base.kb_personal_website_1.arn}/data-source/*"]
  }

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["*"]

  }
}


resource "aws_iam_policy" "lambda_execution_policy_personal_website" {
  name   = "lambda-execution-policy-personal-website"
  policy = data.aws_iam_policy_document.lambda_execution_policy_document_personal_website.json
}

resource "aws_iam_role_policy_attachment" "attach_lambda_execution_role_personal_website" {
  role       = aws_iam_role.lambda_execution_role_personal_website.name
  policy_arn = aws_iam_policy.lambda_execution_policy_personal_website.arn
}

# Lambda access to Source and Destinations (Staging bucket)
data "aws_iam_policy_document" "lambda_s3_access_source_dest_bucket" {
  statement {
    sid    = "ReadFromSourceBucket"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:ListBucket"
    ]

    resources = [
      var.frontend_bucket_arn,
      "${var.frontend_bucket_arn}/*"
    ]
  }

  statement {
    sid    = "WriteToStagingBucket"
    effect = "Allow"

    actions = [
      "s3:PutObject",
      "s3:PutObjectAcl",
      "s3:ListBucket"
    ]

    resources = [
      aws_s3_bucket.bedrock_s3bucket_staging.arn,
      "${aws_s3_bucket.bedrock_s3bucket_staging.arn}/*"
    ]
  }
}

resource "aws_iam_policy" "lambda_s3_access_source_dest_bucket" {
  name        = "lambda-s3-access-policy_source_dest_bucket"
  description = "Allow Lambda to read from source bucket and write to staging bucket"
  policy      = data.aws_iam_policy_document.lambda_s3_access_source_dest_bucket.json
}

# IAM Role Policy Attachment
resource "aws_iam_role_policy_attachment" "lambda_s3_access_attach" {
  role       = aws_iam_role.lambda_execution_role_personal_website.name
  policy_arn = aws_iam_policy.lambda_s3_access_source_dest_bucket.arn
}

# Lambda role polcy attachment for S3 vector database access

data "aws_iam_policy_document" "lambda_s3vectors_getindex" {
  statement {
    sid    = "AllowGetS3VectorIndex"
    effect = "Allow"
    actions = [
      "s3vectors:GetIndex",
    ]
    resources = [
      "arn:aws:s3vectors:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:bucket/${aws_s3vectors_vector_bucket.kb_s3_vector_personal_website.vector_bucket_name}/index/${aws_s3vectors_index.kb_s3_vector_index_personal_website.index_name}",
    ]
  }
}

resource "aws_iam_policy" "lambda_s3vectors_getindex" {
  name   = "lambda-bedrock-s3vectors-getindex-personal-website"
  policy = data.aws_iam_policy_document.lambda_s3vectors_getindex.json
}

resource "aws_iam_role_policy_attachment" "lambda_s3vectors_getindex" {
  role       = aws_iam_role.lambda_execution_role_personal_website.name   
  policy_arn = aws_iam_policy.lambda_s3vectors_getindex.arn
}


# Package the Lambda function code
data "archive_file" "lambda_bedrock_invocation_code_personal_website" {
  type        = "zip"
  source_file = "${path.module}/../Code/lambda_kb_processing/app.py"
  output_path = "${path.module}/../Code/lambda_kb_processing/function.zip"
}

# Lambda function

resource "aws_lambda_layer_version" "pdfminer_layer" {
  filename            = "${path.module}/../Code/lambda_kb_processing/pdfminer_layer.zip"
  layer_name          = "pdfminer-layer"
  compatible_runtimes = ["python3.13"]

  description = "PDF parsing library for resume ingestion"
}



resource "aws_lambda_function" "lambda_bedrock_function_personal_website" {
  filename      = data.archive_file.lambda_bedrock_invocation_code_personal_website.output_path
  function_name = "lambda_bedrock_function_personal_website"
  role          = aws_iam_role.lambda_execution_role_personal_website.arn
  handler       = "app.handler"
  code_sha256   = data.archive_file.lambda_bedrock_invocation_code_personal_website.output_base64sha256
  layers        = [aws_lambda_layer_version.pdfminer_layer.arn]

  runtime = "python3.13"

  timeout     = 120
  memory_size = 512


  environment {
    variables = {
      ENVIRONMENT             = "Personal Website"
      LOG_LEVEL               = "info"
      KNOWLEDGE_BASE_ID       = "${aws_bedrockagent_knowledge_base.kb_personal_website_1.id}"
      DATA_SOURCE_ID          = "${aws_bedrockagent_data_source.bedrock_data_source_personal_website_1.data_source_id}"
      REGION                  = "${data.aws_region.current.region}"
      FRONTEND_BUCKET         = var.frontend_bucket_name
      STAGING_BUCKET          = "${aws_s3_bucket.bedrock_s3bucket_staging.bucket}"
      STAGING_PREFIX          = "bedrock-clean"
      QUARANTINE_PREFIX       = "quarantine"
      MAX_CHUNK_BYTES         = "1800"
      DRY_RUN                 = "false"
      CHUNK_OVERLAP_BYTES     = "200"
      VECTOR_BUCKET_NAME   = "${aws_s3vectors_vector_bucket.kb_s3_vector_personal_website.vector_bucket_name}"
      VECTOR_INDEX_NAME  = "${aws_s3vectors_index.kb_s3_vector_index_personal_website.index_name}"
    }
  }

  tags = {
    Environment = "Personal Website"
    Application = "Personal Website Chat Agent"
  }
}





data "aws_iam_policy_document" "lambda_s3_read_staging" {
  statement {
    sid       = "AllowListStagingBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.bedrock_s3bucket_staging.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["bedrock-clean/*", "bedrock-clean"]
    }
  }

  statement {
    sid       = "AllowGetObjectsFromStaging"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.bedrock_s3bucket_staging.arn}/bedrock-clean/*"]
  }
}

resource "aws_iam_policy" "lambda_read_staging_policy" {
  name   = "lambda-read-staging-${data.aws_caller_identity.current.account_id}"
  policy = data.aws_iam_policy_document.lambda_s3_read_staging.json
}

resource "aws_iam_role_policy_attachment" "attach_lambda_read_staging" {
  role       = aws_iam_role.lambda_execution_role_personal_website.name
  policy_arn = aws_iam_policy.lambda_read_staging_policy.arn
}



# CloudWatch resource
resource "aws_cloudwatch_log_group" "lambda_bedrock_kb_logs_personal_website" {
  name              = "/aws/lambda/${aws_lambda_function.lambda_bedrock_function_personal_website.function_name}"
  retention_in_days = 14
}




