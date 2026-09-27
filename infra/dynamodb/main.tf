resource "aws_dynamodb_table" "mail" {
  name         = "${var.app_name}-mail"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}