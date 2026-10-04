# The cluster's notebook lives in the bootstrap bucket, next to bootstrap's
# own, under a different key. Destroying the cluster empties this notebook;
# the bucket itself is protected in bootstrap (prevent_destroy).

terraform {
  backend "oci" {
    bucket              = "ai-platform-tfstate"
    namespace           = "bmgwrldtzzao"
    key                 = "cluster/terraform.tfstate"
    region              = "ap-mumbai-1"
    auth                = "SecurityToken"
    config_file_profile = "DEFAULT"
  }
}
