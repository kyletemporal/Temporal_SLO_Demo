# Layer 1 of 2: AWS infrastructure only.
#
# This root module creates the VPC, the EKS cluster and the RDS database, and
# stops there. It does NOT install anything into Kubernetes — no helm_release,
# no kubernetes_* resources. Layer 2 (../helm, driven by ../scripts/deploy.sh)
# does that.
#
# The split is deliberate. Configuring the kubernetes/helm providers from the
# outputs of an EKS cluster created in the SAME apply is the pattern HashiCorp's
# own provider docs warn against: on the first plan the cluster does not exist,
# so the providers are configured with unknown values, and on later plans a
# cluster replacement leaves them pointing at the old endpoint. It fails in ways
# that look like auth errors and are really ordering errors.
#
# It also matches how ownership usually splits: a platform team owns the
# account-level infrastructure, the Temporal operators own the Helm release and
# upgrade it on their own cadence. A server upgrade should never need a
# `terraform apply` against the VPC.

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}

# -----------------------------------------------------------------------------
# VPC — three AZs, private subnets for nodes, isolated subnets for RDS.
# -----------------------------------------------------------------------------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = var.name
  cidr = var.vpc_cidr
  azs  = local.azs

  # /20 private subnets: VPC CNI gives every pod a VPC IP, so node subnets run
  # out of addresses long before they run out of anything else.
  private_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets   = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 48 + i)]
  database_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 64 + i)]

  create_database_subnet_group = true

  enable_nat_gateway     = true
  single_nat_gateway     = var.single_nat_gateway
  one_nat_gateway_per_az = !var.single_nat_gateway

  enable_dns_hostnames = true

  # Lets an internal NLB for the Temporal frontend land in the private subnets
  # if one is enabled later (see ../README.md, "Reaching the frontend").
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
}

# -----------------------------------------------------------------------------
# EKS
# -----------------------------------------------------------------------------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Public endpoint, but only from admin_cidrs; nodes and pods use the private
  # endpoint. A fully private endpoint is better and needs a VPN or bastion,
  # which is more than a test environment usually has.
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.admin_cidrs
  endpoint_private_access      = true

  # Whoever applies this gets cluster-admin through an access entry, not the
  # aws-auth ConfigMap. Add more principals with `access_entries`.
  enable_cluster_creator_admin_permissions = true

  addons = {
    coredns = {}
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
    }
    # Prometheus keeps its TSDB on a gp3 volume, so the CSI driver is not
    # optional. Credentials come from EKS Pod Identity, not node-role policy.
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = module.ebs_csi_pod_identity.iam_role_arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.node_instance_types

      min_size     = var.node_count
      desired_size = var.node_count
      max_size     = var.node_max_count

      disk_size = 50

      labels = {
        "temporal-selfhosted/pool" = "default"
      }
    }
  }
}

module "ebs_csi_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.0"

  name = "${var.name}-ebs-csi"

  attach_aws_ebs_csi_policy = true
}

# -----------------------------------------------------------------------------
# Temporal's application database credential.
#
# Only the secret CONTAINER is created here. scripts/deploy.sh generates the
# password and writes the first version, so the value never passes through
# Terraform state — the same reason the RDS master password is managed by RDS
# itself (see rds.tf).
# -----------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "temporal_db" {
  name_prefix             = "${var.name}-temporal-db-"
  description             = "Password for the `temporal` PostgreSQL role. Written by scripts/deploy.sh."
  recovery_window_in_days = 7
}
