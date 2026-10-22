provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "amazon-connect-lab"
      Environment = "lab"
      ManagedBy   = "terraform"
      Repo        = "amazon-connect-lab"
    }
  }
}

# Reserved for future tagging/account-aware resources.
# data "aws_caller_identity" "current" {}

# Reserved for future region-aware resources.
# data "aws_region" "current" {}
