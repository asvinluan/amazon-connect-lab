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

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
