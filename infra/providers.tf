terraform {
  required_version = ">= 1.6"

  required_providers {
    # The AWS provider turns HCL resources into AWS API calls.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70, < 7.0"
    }
    # The archive provider zips src/ locally so Lambda gets a deployment package.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# Credentials come from your AWS CLI profile (IAM Identity Center works too),
# so no keys ever live in this repo.
provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  # Every taggable resource gets these tags, which makes cost allocation and
  # cleanup easy.
  default_tags {
    tags = {
      Project   = "CloudBillWatchdog"
      ManagedBy = "Terraform"
    }
  }
}
