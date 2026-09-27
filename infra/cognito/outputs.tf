output "app_client_id" {
  value = aws_cognito_user_pool_client.google_user_pool_client.id
}

output "discovery_url" {
  value = "https://${aws_cognito_user_pool.google_idp_pool.endpoint}/.well-known/openid-configuration"
}

output "issuer_url" {
  value = "https://${aws_cognito_user_pool.google_idp_pool.endpoint}"
}

output "userinfo_url" {
  description = "Cognito endpoint for retrieving authenticated user attributes."

  value = "https://${aws_cognito_user_pool_domain.google_domain.domain}.auth.${data.aws_region.current.region}.amazoncognito.com/oauth2/userInfo"
}