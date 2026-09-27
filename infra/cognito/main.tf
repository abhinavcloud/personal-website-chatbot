data "aws_region" "current" {}

# Creating Cognito User Pool and User Pool Client for Google Sign In
resource "aws_cognito_user_pool" "google_idp_pool" {
  name                     = "google-idp-pool"
  auto_verified_attributes = ["email"]
}


# Creating the Identity Provider with Cognito for Google
resource "aws_cognito_identity_provider" "google_idp" {
  user_pool_id  = aws_cognito_user_pool.google_idp_pool.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    authorize_scopes = "openid email profile"
    client_id        = var.client_id
    client_secret    = var.client_secret
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
    email_verified = "email_verified"
  }
}



resource "aws_cognito_user_pool_client" "google_user_pool_client" {
  name                                 = "google-user-pool-client"
  user_pool_id                         = aws_cognito_user_pool.google_idp_pool.id

  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["email", "openid"]

  # Callback URLs for Hosted UI (after successful authentication)
  callback_urls                        = ["http://localhost:3000/callback.html"]
  # Logout URLs for Hosted UI (after sign out)
  logout_urls                          = ["http://localhost:3000/index.html"]
  default_redirect_uri = "http://localhost:3000/callback.html"

  supported_identity_providers         = ["Google"]

  depends_on = [aws_cognito_identity_provider.google_idp]
}


# Creating a Cognito User Pool Domain for Hosted UI which is used for Google Sign In. 
# This is the application domain registered with Google for Oauth.
resource "aws_cognito_user_pool_domain" "google_domain" {
  domain       = "${var.app_name}-auth"  # must be globally unique
  user_pool_id = aws_cognito_user_pool.google_idp_pool.id

}
