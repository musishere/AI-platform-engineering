# Bootstrap: the permanent, free foundation for everything else in Oracle Cloud.
#
# Lives apart from infra/cluster on purpose: `terraform destroy` there (every
# session) must never be able to delete the two things that protect us, the
# state bucket (Terraform's record of what it built) and the budget alerts.
#
# Run: terraform -chdir=infra/bootstrap plan / apply
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

variable "tenancy_ocid" {
  description = "The account's root ID (from ~/.oci/config)."
  type        = string
}

variable "alert_email" {
  description = "Where budget alerts go."
  type        = string
}

variable "region" {
  description = "Home region. Always Free resources only exist here."
  type        = string
  default     = "ap-mumbai-1"
}

provider "oci" {
  # Short-lived browser session instead of a permanent API key file: a leaked
  # session stops working within the hour, a leaked key until someone notices.
  auth                = "SecurityToken"
  config_file_profile = "DEFAULT"
  region              = var.region
}

# One folder for everything this project creates, so it's easy to see what
# exists and nothing mixes with the rest of the account.
resource "oci_identity_compartment" "project" {
  compartment_id = var.tenancy_ocid
  name           = "ai-platform"
  description    = "AI platform engineering learning project"
}

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.tenancy_ocid
}

# Terraform's notebook (remote state) for every part of this project.
resource "oci_objectstorage_bucket" "tfstate" {
  compartment_id = oci_identity_compartment.project.id
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = "ai-platform-tfstate"
  # The notebook describes the whole setup, so it's never public.
  access_type = "NoPublicAccess"
  # Every save keeps the previous copy, so a damaged notebook can be rolled
  # back instead of losing track of resources that are still billing.
  versioning = "Enabled"

  lifecycle {
    # Deleting this would orphan every resource it tracks. Terraform refuses
    # to destroy it; removing this line is a deliberate, visible act.
    prevent_destroy = true
  }
}

# Budget over the WHOLE account (root compartment), not just the project
# folder, so nothing can be created somewhere else and slip past the alerts.
# Trial credit usage counts as spend, so alerts fire as the credit is used.
resource "oci_budget_budget" "monthly" {
  compartment_id = var.tenancy_ocid
  display_name   = "monthly-safety-net"
  amount         = 50
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [var.tenancy_ocid]
}

resource "oci_budget_alert_rule" "at_20" {
  budget_id      = oci_budget_budget.monthly.id
  display_name   = "actual-over-20"
  type           = "ACTUAL"
  threshold_type = "ABSOLUTE"
  threshold      = 20
  recipients     = var.alert_email
  message        = "OCI spend passed $20 this month. Check nothing is left running (terraform destroy in infra/cluster)."
}

resource "oci_budget_alert_rule" "at_50" {
  budget_id      = oci_budget_budget.monthly.id
  display_name   = "actual-over-50"
  type           = "ACTUAL"
  threshold_type = "ABSOLUTE"
  threshold      = 50
  recipients     = var.alert_email
  message        = "OCI spend passed $50 this month. Stop and destroy everything in infra/cluster now."
}

output "compartment_id" {
  value = oci_identity_compartment.project.id
}

output "state_bucket" {
  value = oci_objectstorage_bucket.tfstate.name
}

output "namespace" {
  value = data.oci_objectstorage_namespace.this.namespace
}
