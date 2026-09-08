#Created by: Abhinav Kumar (abhinav@abhinav-cloud.com)
#Version 1.0

terraform {
  required_version = ">= 1.14.0"
 
  backend "s3" {
  }

  
  required_providers {
    template = {
      source  = "hashicorp/template"
      version = "2.2.0"
    }

    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.24.0"
    }


    }
}