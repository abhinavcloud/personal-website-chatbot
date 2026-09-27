
resource "aws_ses_email_identity" "mail" {
    email = var.assistant_email
}

resource "aws_ses_domain_identity" "website" {
  domain = var.website
}

resource "aws_ses_domain_dkim" "website" {
  domain = aws_ses_domain_identity.website.domain
}

