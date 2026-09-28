# Where bootstrap keeps its own notebook: in the bucket it created.
#
# Chicken-and-egg: the bucket didn't exist on the first apply, so that run
# used a local file. After it, `terraform init -migrate-state` moved the
# notebook here, and the local copy was deleted. Nothing important lives only
# on a laptop.
#
# The OCI backend locks the notebook while a run is in progress (a lock object
# next to it), so two runs at once can't half-overwrite each other.

terraform {
  backend "oci" {
    bucket              = "ai-platform-tfstate"
    namespace           = "bmgwrldtzzao"
    key                 = "bootstrap/terraform.tfstate"
    region              = "ap-mumbai-1"
    auth                = "SecurityToken"
    config_file_profile = "DEFAULT"
  }
}
