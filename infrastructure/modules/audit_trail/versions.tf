terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
      # The replica provider is a different region; the caller supplies it.
      configuration_aliases = [aws.replica]
    }
  }
}
