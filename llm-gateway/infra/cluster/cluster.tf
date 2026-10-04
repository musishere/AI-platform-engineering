# OKE cluster: the Kubernetes brain (control plane, run by Oracle) plus a node
# pool of 2 ARM worker machines on the private workers floor.
#
# Basic cluster for now: its control plane is free. The Workload Identity step
# switches `type` to ENHANCED_CLUSTER (paid from the trial credit), since
# workload identity only exists on enhanced clusters. We rebuild every
# session anyway, so that switch is a one-line change, not a migration.

locals {
  # Pinned, not "latest": a rebuild next week gives the same cluster, and an
  # upgrade is a deliberate, reviewable one-line change. Newest OKE offered
  # on 2026-09-28.
  kubernetes_version = "v1.36.1"

  # Newest Oracle Linux 9 ARM (aarch64) image built for this exact Kubernetes
  # version. Chosen by filter rather than hard-coded, so each rebuild picks up
  # Oracle's latest security-patched image for the same version.
  node_image_id = [
    for source in reverse(sort([
      for s in data.oci_containerengine_node_pool_option.all.sources : "${s.source_name}|${s.image_id}"
      if can(regex("^Oracle-Linux-9\\.[0-9]+-aarch64-.*-OKE-${trimprefix(local.kubernetes_version, "v")}-", s.source_name))
    ])) : split("|", source)[1]
  ][0]
}

data "oci_containerengine_node_pool_option" "all" {
  node_pool_option_id = "all"
  compartment_id      = local.compartment_id
}

# Mumbai has a single availability domain (data centre); machines are still
# spread across its fault domains (separate racks/power) automatically.
data "oci_identity_availability_domains" "all" {
  compartment_id = local.compartment_id
}

resource "oci_containerengine_cluster" "main" {
  compartment_id     = local.compartment_id
  name               = "ai-platform"
  kubernetes_version = local.kubernetes_version
  vcn_id             = oci_core_vcn.main.id
  type               = "BASIC_CLUSTER"

  # Overlay pod networking: pods get addresses from an internal range, so
  # they don't consume VCN addresses. Matches the security lists in network.tf.
  cluster_pod_network_options {
    cni_type = "FLANNEL_OVERLAY"
  }

  # The Kubernetes API endpoint sits on the public API floor so kubectl works
  # from a laptop. Every call still needs a token from an Oracle login.
  endpoint_config {
    subnet_id            = oci_core_subnet.api.id
    is_public_ip_enabled = true
  }

  options {
    # Where Oracle creates load balancers when a Kubernetes Service asks for
    # one (the customer front door, used when we expose the gateway).
    service_lb_subnet_ids = [oci_core_subnet.lb.id]
    kubernetes_network_config {
      pods_cidr     = "10.244.0.0/16" # Kubernetes' usual defaults; must not overlap the VCN
      services_cidr = "10.96.0.0/16"
    }
  }
}

resource "oci_containerengine_node_pool" "workers" {
  cluster_id         = oci_containerengine_cluster.main.id
  compartment_id     = local.compartment_id
  name               = "workers"
  kubernetes_version = local.kubernetes_version

  # Ampere A1 (ARM): the Always Free allowance is 2 OCPUs + 12 GB in total.
  # Split into 2 small machines rather than 1 big one: if one dies, the other
  # keeps running and Kubernetes moves pods across.
  node_shape = "VM.Standard.A1.Flex"
  node_shape_config {
    ocpus         = 1
    memory_in_gbs = 6
  }

  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.node_image_id
    boot_volume_size_in_gbs = 50 # 2 x 50 GB, inside the 200 GB free allowance
  }

  node_config_details {
    size = 2
    placement_configs {
      availability_domain = data.oci_identity_availability_domains.all.availability_domains[0].name
      # Private workers floor: no public addresses, out to the internet via NAT.
      subnet_id = oci_core_subnet.workers.id
    }
    node_pool_pod_network_option_details {
      cni_type = "FLANNEL_OVERLAY"
    }
  }
}

output "cluster_id" {
  value = oci_containerengine_cluster.main.id
}
