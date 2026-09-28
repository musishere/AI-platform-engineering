# Network (VCN): the private "building" the Kubernetes cluster lives in.
#
# Three floors (subnets):
#   API endpoint  10.0.0.0/28   PUBLIC   the cluster brain's front desk (kubectl)
#   workers       10.0.10.0/24  PRIVATE  machines running the gateway, Postgres,
#                                        Redis; out via NAT, no way in from outside
#   load balancer 10.0.20.0/24  PUBLIC   the front door callers will use
#
# Layout and every security rule follow Oracle's reference example for OKE
# with flannel (overlay) pod networking, a public API endpoint, private
# workers and public load balancers:
# https://docs.oracle.com/en-us/iaas/Content/ContEng/Concepts/contengnetworkconfigexample.htm
# Pods get addresses from an overlay range inside the cluster, so no subnet
# for pods is needed here.

locals {
  vcn_cidr     = "10.0.0.0/16"  # ~65k addresses for the whole building
  api_cidr     = "10.0.0.0/28"  # 16 addresses; the API endpoint needs 1 (OCI reserves 3)
  workers_cidr = "10.0.10.0/24" # 256 addresses; ranges don't overlap, so floors never mix
  lb_cidr      = "10.0.20.0/24"
  anywhere     = "0.0.0.0/0"

  # OCI identifies protocols by number.
  tcp  = "6"
  icmp = "1"
  all  = "all"

  oracle_services = data.oci_core_services.all.services[0]
}

# "All <region> Services In Oracle Services Network": Object Storage, the image
# registry, OKE itself. Reached through the service gateway, never the internet.
data "oci_core_services" "all" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_vcn" "main" {
  compartment_id = local.compartment_id
  display_name   = "ai-platform-vcn"
  cidr_blocks    = [local.vcn_cidr]
  # Lets machines find each other by name inside the VCN (OKE needs it).
  dns_label = "aiplatform"
}

# --- Doors -------------------------------------------------------------------

# Front door: two-way internet access, only for the PUBLIC floors.
resource "oci_core_internet_gateway" "igw" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "internet-gateway"
  enabled        = true
}

# Staff exit with one-way doors: private workers can go OUT (pull images,
# call OpenRouter), but nothing on the internet can start a connection IN.
resource "oci_core_nat_gateway" "nat" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "nat-gateway"
}

# Private corridor to Oracle's own services, without crossing the internet.
resource "oci_core_service_gateway" "sgw" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "service-gateway"
  services {
    service_id = local.oracle_services.id
  }
}

# --- Signs in the corridors (route tables) --------------------------------------

resource "oci_core_route_table" "public" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "public-routes"
  route_rules {
    description       = "Everything outside the VCN goes via the internet gateway."
    destination       = local.anywhere
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "private-routes"
  route_rules {
    description       = "Internet traffic leaves via NAT (out only)."
    destination       = local.anywhere
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.nat.id
  }
  route_rules {
    description       = "Oracle services go via the private service gateway."
    destination       = local.oracle_services.cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.sgw.id
  }
}

# --- Guards at each floor's door (security lists) ------------------------------
# "Stateful" rules (the default): if a request is allowed out, its reply is
# automatically allowed back in, so each rule only describes who STARTS talking.
# ICMP type 3 code 4 is "packet too big": without it, some connections hang
# silently when a large packet can't fit (path MTU discovery).

resource "oci_core_security_list" "api" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "seclist-api-endpoint"

  ingress_security_rules {
    description = "Workers talk to the Kubernetes API."
    protocol    = local.tcp
    source      = local.workers_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  ingress_security_rules {
    description = "Workers talk to the control plane."
    protocol    = local.tcp
    source      = local.workers_cidr
    tcp_options {
      min = 12250
      max = 12250
    }
  }
  ingress_security_rules {
    description = "Path discovery from workers."
    protocol    = local.icmp
    source      = local.workers_cidr
    icmp_options {
      type = 3
      code = 4
    }
  }
  ingress_security_rules {
    # Public so kubectl works from a laptop. Still requires an Oracle login;
    # reachable is not the same as usable. Tradeoff vs a private endpoint +
    # bastion: much simpler, slightly larger attack surface.
    description = "kubectl from outside (authenticated by OCI)."
    protocol    = local.tcp
    source      = local.anywhere
    tcp_options {
      min = 6443
      max = 6443
    }
  }

  egress_security_rules {
    description      = "Control plane talks to OKE services."
    protocol         = local.tcp
    destination      = local.oracle_services.cidr_block
    destination_type = "SERVICE_CIDR_BLOCK"
  }
  egress_security_rules {
    description      = "Path discovery to Oracle services."
    protocol         = local.icmp
    destination      = local.oracle_services.cidr_block
    destination_type = "SERVICE_CIDR_BLOCK"
    icmp_options {
      type = 3
      code = 4
    }
  }
  egress_security_rules {
    description = "Control plane talks to workers."
    protocol    = local.tcp
    destination = local.workers_cidr
  }
  egress_security_rules {
    description = "Path discovery to workers."
    protocol    = local.icmp
    destination = local.workers_cidr
    icmp_options {
      type = 3
      code = 4
    }
  }
}

resource "oci_core_security_list" "workers" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "seclist-workers"

  ingress_security_rules {
    description = "Pods on one worker talk to pods on another."
    protocol    = local.all
    source      = local.workers_cidr
  }
  ingress_security_rules {
    description = "Control plane talks to workers."
    protocol    = local.tcp
    source      = local.api_cidr
  }
  ingress_security_rules {
    description = "Path discovery."
    protocol    = local.icmp
    source      = local.anywhere
    icmp_options {
      type = 3
      code = 4
    }
  }
  # Oracle's example says "ALL" protocols on these ports; ports only exist for
  # TCP/UDP, and both NodePorts and the kube-proxy health check are TCP here.
  ingress_security_rules {
    description = "Load balancer reaches services on node ports."
    protocol    = local.tcp
    source      = local.lb_cidr
    tcp_options {
      min = 30000
      max = 32767
    }
  }
  ingress_security_rules {
    description = "Load balancer health-checks kube-proxy."
    protocol    = local.tcp
    source      = local.lb_cidr
    tcp_options {
      min = 10256
      max = 10256
    }
  }

  egress_security_rules {
    description = "Pods on one worker talk to pods on another."
    protocol    = local.all
    destination = local.workers_cidr
  }
  egress_security_rules {
    description = "Path discovery."
    protocol    = local.icmp
    destination = local.anywhere
    icmp_options {
      type = 3
      code = 4
    }
  }
  egress_security_rules {
    description      = "Workers talk to OKE services."
    protocol         = local.tcp
    destination      = local.oracle_services.cidr_block
    destination_type = "SERVICE_CIDR_BLOCK"
  }
  egress_security_rules {
    description = "Workers talk to the Kubernetes API."
    protocol    = local.tcp
    destination = local.api_cidr
    tcp_options {
      min = 6443
      max = 6443
    }
  }
  egress_security_rules {
    description = "Workers talk to the control plane."
    protocol    = local.tcp
    destination = local.api_cidr
    tcp_options {
      min = 12250
      max = 12250
    }
  }
  egress_security_rules {
    # Needed for pulling images and for the gateway calling OpenRouter.
    # Goes out through NAT; nothing can come in this way.
    description = "Workers reach the internet (via NAT)."
    protocol    = local.tcp
    destination = local.anywhere
  }
}

resource "oci_core_security_list" "lb" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "seclist-load-balancers"

  # No ingress yet on purpose: the listener port (who may reach the gateway
  # from the internet) is added when we actually expose the gateway. Until
  # then, nothing can come in the front door.

  egress_security_rules {
    description = "Load balancer reaches services on node ports."
    protocol    = local.tcp
    destination = local.workers_cidr
    tcp_options {
      min = 30000
      max = 32767
    }
  }
  egress_security_rules {
    description = "Load balancer health-checks kube-proxy."
    protocol    = local.tcp
    destination = local.workers_cidr
    tcp_options {
      min = 10256
      max = 10256
    }
  }
}

# --- Floors (subnets) ------------------------------------------------------------
# Regional (no availability_domain): a subnet spans the whole region, so the
# cluster isn't tied to one data centre.

resource "oci_core_subnet" "api" {
  compartment_id    = local.compartment_id
  vcn_id            = oci_core_vcn.main.id
  display_name      = "subnet-api-endpoint"
  cidr_block        = local.api_cidr
  dns_label         = "kapi"
  route_table_id    = oci_core_route_table.public.id
  security_list_ids = [oci_core_security_list.api.id]
}

resource "oci_core_subnet" "workers" {
  compartment_id    = local.compartment_id
  vcn_id            = oci_core_vcn.main.id
  display_name      = "subnet-workers"
  cidr_block        = local.workers_cidr
  dns_label         = "workers"
  route_table_id    = oci_core_route_table.private.id
  security_list_ids = [oci_core_security_list.workers.id]
  # This is what makes the floor PRIVATE: no machine here can get a public
  # address, so there's nothing on the internet to connect to.
  prohibit_public_ip_on_vnic = true
}

resource "oci_core_subnet" "lb" {
  compartment_id    = local.compartment_id
  vcn_id            = oci_core_vcn.main.id
  display_name      = "subnet-load-balancers"
  cidr_block        = local.lb_cidr
  dns_label         = "lbs"
  route_table_id    = oci_core_route_table.public.id
  security_list_ids = [oci_core_security_list.lb.id]
}

output "vcn_id" {
  value = oci_core_vcn.main.id
}

output "subnet_ids" {
  value = {
    api     = oci_core_subnet.api.id
    workers = oci_core_subnet.workers.id
    lb      = oci_core_subnet.lb.id
  }
}
