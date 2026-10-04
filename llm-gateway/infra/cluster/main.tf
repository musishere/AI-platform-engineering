# Cluster: the per-session part of the Oracle Cloud setup (network now, the
# OKE cluster and Workload Identity next).
#
# Unlike infra/bootstrap, everything here is built, tested and DESTROYED each
# session:  terraform -chdir=infra/cluster destroy
# Login first (lasts ~1h): oci session authenticate --region ap-mumbai-1 --profile-name DEFAULT

terraform {
  required_version = ">= 1.16"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 9.3"
    }
  }
}

variable "region" {
  description = "Home region. Always Free resources only exist here."
  type        = string
  default     = "ap-mumbai-1"
}

provider "oci" {
  # Short-lived browser session, same as bootstrap: no permanent key file.
  auth                = "SecurityToken"
  config_file_profile = "DEFAULT"
  region              = var.region
}

# Read bootstrap's outputs (the project compartment) straight from its
# notebook, instead of copy-pasting IDs: the two folders can't drift apart.
data "terraform_remote_state" "bootstrap" {
  backend = "oci"
  config = {
    bucket              = "ai-platform-tfstate"
    namespace           = "bmgwrldtzzao"
    key                 = "bootstrap/terraform.tfstate"
    region              = "ap-mumbai-1"
    auth                = "SecurityToken"
    config_file_profile = "DEFAULT"
  }
}

locals {
  compartment_id = data.terraform_remote_state.bootstrap.outputs.compartment_id
}
