terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Pinned. An unpinned provider upgrade is an unreviewed change
      # to production, which is exactly what this repo exists to prevent.
      version = "~> 5.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # Local state is fine for a lab. For anything shared, use the S3 backend
  # below: remote state, DynamoDB locking so two applies cannot corrupt it,
  # and KMS encryption because state holds resource identifiers.
  #
  # backend "s3" {
  #   bucket         = "CHANGEME-tfstate"
  #   key            = "connect/lab/terraform.tfstate"
  #   region         = "ap-southeast-2"
  #   dynamodb_table = "CHANGEME-tflock"
  #   encrypt        = true
  # }
}
