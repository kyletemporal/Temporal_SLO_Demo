#!/usr/bin/env bash
# Deploy the self-hosted Temporal environment from AWS CloudShell.
#
#   curl -fsSLO https://raw.githubusercontent.com/kyletemporal/Temporal_SLO_Demo/main/aws/eks-selfhosted/scripts/cloudshell.sh
#   bash cloudshell.sh up
#
# Commands:
#   up       everything: tools, repo, state bucket, terraform apply, deploy, smoke test
#   deploy   re-run only the Helm layer (after editing values, or after a dropped session)
#   smoke    run the end-to-end smoke test
#   status   pods, Helm releases, and where things are
#   connect  refresh kubeconfig and this session's IP on the API allowlist
#   down     tear everything down (asks for confirmation)
#
# Environment (all optional):
#   ENV_NAME            environment / cluster name        default temporal-selfhosted
#   AWS_REGION          region                            default: CloudShell's region
#   EXTRA_ADMIN_CIDRS   more CIDRs for the EKS API, comma-separated — add your
#                       laptop's /32 here if you want kubectl / port-forward
#                       from it (CloudShell cannot open the UI or Grafana)
#   NUM_HISTORY_SHARDS  first deploy only; permanent         default 512
#   REPO_REF            git branch/tag to deploy             default main
#
# Why it is shaped like this — four CloudShell facts:
#
# 1. Sessions end after ~20-30 minutes WITHOUT KEYBOARD INPUT, and EKS takes
#    ~15-20 minutes to create. Terraform state therefore lives in S3 with
#    locking, not in the session: if the session drops mid-apply, open a new
#    one and run the same command. Terraform picks up where it stopped.
# 2. $HOME is capped at 1 GB. The AWS provider alone is several hundred MB, so
#    Terraform's working directory (.terraform) goes to /tmp. That means
#    re-initialising each session, which this script does automatically.
# 3. Each session gets a different public IP. The EKS API only accepts
#    admin_cidrs, so every run adds the current session's /32 and applies the
#    change before talking to the cluster.
# 4. CloudShell's credentials are whoever is signed in to the console. That
#    principal becomes cluster admin, needs rights to create VPC/EKS/RDS/IAM/
#    Secrets Manager/S3, and is the only one with kubectl access until you add
#    more access entries.
set -euo pipefail

ENV_NAME="${ENV_NAME:-temporal-selfhosted}"
REPO_URL="https://github.com/kyletemporal/Temporal_SLO_Demo.git"
REPO_REF="${REPO_REF:-main}"
REPO_DIR="${REPO_DIR:-$HOME/Temporal_SLO_Demo}"

TERRAFORM_VERSION="1.15.8"
HELM_VERSION="4.2.3"
KUBECTL_VERSION="1.36.1"     # within one minor of the EKS version in terraform/variables.tf
BIN="$HOME/.local/bin"
export PATH="$BIN:$PATH"

# .terraform in /tmp, not $HOME — see note 2 above.
export TF_DATA_DIR="/tmp/tfdata-$ENV_NAME"
export TF_IN_AUTOMATION=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Tools — pinned and checksum-verified, installed once into ~/.local/bin
# (persists across sessions; ~190 MB of the 1 GB home).
# -----------------------------------------------------------------------------
arch() {
  case "$(uname -m)" in
    x86_64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
}

have_version() { command -v "$1" >/dev/null 2>&1 && "$@" 2>/dev/null | grep -q "$VERSION_WANT"; }

install_tools() {
  mkdir -p "$BIN"
  local a tmp; a="$(arch)"; tmp="$(mktemp -d)"

  VERSION_WANT="$TERRAFORM_VERSION"
  if ! have_version terraform version; then
    say "installing terraform $TERRAFORM_VERSION"
    local zip="terraform_${TERRAFORM_VERSION}_linux_${a}.zip"
    curl -fsSL -o "$tmp/$zip" "https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION/$zip"
    curl -fsSL -o "$tmp/sums" "https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION/terraform_${TERRAFORM_VERSION}_SHA256SUMS"
    (cd "$tmp" && grep " $zip\$" sums | sha256sum -c - >/dev/null) || die "terraform checksum mismatch"
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('terraform', sys.argv[2])" "$tmp/$zip" "$BIN"
    chmod +x "$BIN/terraform"
  fi

  VERSION_WANT="v$HELM_VERSION"
  if ! have_version helm version --short; then
    say "installing helm $HELM_VERSION"
    local tgz="helm-v${HELM_VERSION}-linux-${a}.tar.gz"
    curl -fsSL -o "$tmp/$tgz" "https://get.helm.sh/$tgz"
    curl -fsSL -o "$tmp/$tgz.sha256sum" "https://get.helm.sh/$tgz.sha256sum"
    (cd "$tmp" && sha256sum -c "$tgz.sha256sum" >/dev/null) || die "helm checksum mismatch"
    tar -xzf "$tmp/$tgz" -C "$tmp"
    install -m 0755 "$tmp/linux-$a/helm" "$BIN/helm"
  fi

  VERSION_WANT="v$KUBECTL_VERSION"
  if ! have_version kubectl version --client; then
    say "installing kubectl $KUBECTL_VERSION"
    local base="https://dl.k8s.io/release/v$KUBECTL_VERSION/bin/linux/$a"
    curl -fsSL -o "$tmp/kubectl" "$base/kubectl"
    echo "$(curl -fsSL "$base/kubectl.sha256")  $tmp/kubectl" | sha256sum -c - >/dev/null || die "kubectl checksum mismatch"
    install -m 0755 "$tmp/kubectl" "$BIN/kubectl"
  fi
  rm -rf "$tmp"

  for t in aws python3 openssl curl git; do
    command -v "$t" >/dev/null || die "$t is missing — this script expects AWS CloudShell or an equivalent shell"
  done
  printf 'terraform %s · helm %s · kubectl %s\n' "$TERRAFORM_VERSION" "$HELM_VERSION" "$KUBECTL_VERSION"
}

# -----------------------------------------------------------------------------
# Repo — shallow clone, so the repo's history (which once held a 58 MB binary)
# does not eat the 1 GB home.
# -----------------------------------------------------------------------------
fetch_repo() {
  if [ -d "$REPO_DIR/.git" ]; then
    say "updating $REPO_DIR ($REPO_REF)"
    git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_REF" -q
    git -C "$REPO_DIR" checkout -q -B "$REPO_REF" FETCH_HEAD
  else
    say "cloning $REPO_URL ($REPO_REF)"
    git clone -q --depth 1 --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
  fi
  # Until the EKS work is merged, main does not have it yet.
  if [ ! -d "$REPO_DIR/aws/eks-selfhosted" ] && [ "$REPO_REF" = main ]; then
    warn "aws/eks-selfhosted is not on main yet — using the eks-selfhosted branch"
    REPO_REF=eks-selfhosted
    git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_REF" -q
    git -C "$REPO_DIR" checkout -q -B "$REPO_REF" FETCH_HEAD
  fi
  ROOT="$REPO_DIR/aws/eks-selfhosted"
  TF_DIR="$ROOT/terraform"
  [ -d "$TF_DIR" ] || die "$TF_DIR not found in $REPO_REF"
}

# -----------------------------------------------------------------------------
# Account, region, state bucket
# -----------------------------------------------------------------------------
aws_context() {
  AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  [ -n "$AWS_REGION" ] || AWS_REGION="$(aws configure get region 2>/dev/null || true)"
  [ -n "$AWS_REGION" ] || die "set AWS_REGION"
  export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" || die "no AWS credentials"
  CALLER="$(aws sts get-caller-identity --query Arn --output text)"
  STATE_BUCKET="${STATE_BUCKET:-tfstate-$ACCOUNT_ID-$AWS_REGION}"
  echo "account $ACCOUNT_ID · region $AWS_REGION · as $CALLER"
}

ensure_state_bucket() {
  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    return
  fi
  say "creating state bucket s3://$STATE_BUCKET (versioned, encrypted, private)"
  if [ "$AWS_REGION" = us-east-1 ]; then
    aws s3api create-bucket --bucket "$STATE_BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$STATE_BUCKET" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  # Versioning is what makes a corrupted or clobbered state recoverable.
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
}

# -----------------------------------------------------------------------------
# Terraform inputs: backend + tfvars, regenerated every run.
# -----------------------------------------------------------------------------
my_ip() { curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]'; }

write_tf_inputs() {
  # An *_override.tf file adds the S3 backend without editing the committed
  # configuration, which stays backend-agnostic for everyone else.
  cat > "$TF_DIR/cloudshell_override.tf" <<'EOF'
# Written by scripts/cloudshell.sh. Not committed.
terraform {
  backend "s3" {}
}
EOF

  local ip cidrs
  ip="$(my_ip)" || die "could not determine this session's public IP"
  cidrs="\"$ip/32\""
  if [ -n "${EXTRA_ADMIN_CIDRS:-}" ]; then
    IFS=',' read -ra extra <<<"$EXTRA_ADMIN_CIDRS"
    for c in "${extra[@]}"; do cidrs="$cidrs, \"$(echo "$c" | tr -d '[:space:]')\""; done
  fi
  cat > "$TF_DIR/terraform.tfvars" <<EOF
# Written by scripts/cloudshell.sh on every run. Edit the script's environment
# variables, not this file. Gitignored.
name        = "$ENV_NAME"
region      = "$AWS_REGION"
admin_cidrs = [$cidrs]
EOF
  echo "EKS API allowlist: [$cidrs]"
}

tf_init() {
  # The pinned providers are a few hundred MB; make sure /tmp can take them.
  local free_mb; free_mb="$(df -Pm /tmp | awk 'NR==2 {print $4}')"
  [ "$free_mb" -gt 1500 ] || warn "only ${free_mb} MB free in /tmp; terraform init may fail"
  say "terraform init (state: s3://$STATE_BUCKET/$ENV_NAME/terraform.tfstate)"
  terraform -chdir="$TF_DIR" init -input=false -reconfigure \
    -backend-config="bucket=$STATE_BUCKET" \
    -backend-config="key=$ENV_NAME/terraform.tfstate" \
    -backend-config="region=$AWS_REGION" \
    -backend-config="encrypt=true" \
    -backend-config="use_lockfile=true" >/dev/null
}

prepare() {
  install_tools
  fetch_repo
  aws_context
  ensure_state_bucket
  write_tf_inputs
  tf_init
}

cluster_exists() {
  aws eks describe-cluster --name "$ENV_NAME" >/dev/null 2>&1
}

# Applies only the allowlist change, so a new session can reach the API
# without re-planning the whole environment.
refresh_allowlist() {
  say "updating the EKS API allowlist for this session"
  # Targets the cluster resource alone: the only change is
  # endpoint_public_access_cidrs, and nothing else should ride along unreviewed.
  terraform -chdir="$TF_DIR" apply -input=false -auto-approve \
    -target='module.eks.aws_eks_cluster.this[0]' >/dev/null
  aws eks update-kubeconfig --name "$ENV_NAME" >/dev/null
  # The endpoint change takes effect asynchronously.
  local _
  for _ in $(seq 1 60); do
    kubectl get --raw /readyz >/dev/null 2>&1 && { echo "cluster API reachable"; return; }
    sleep 10
  done
  die "cluster API still unreachable from $(my_ip) after 10 minutes"
}

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
cmd_up() {
  prepare

  cat <<EOF

This creates, in $AWS_REGION:
  VPC + NAT gateway, EKS $ENV_NAME (3 x m7i.xlarge), RDS PostgreSQL 16 (db.m7g.large)
Roughly \$0.95/hour (~\$700/month) at list price until you run: bash $0 down

Terraform will show the full plan and ask before changing anything.
Keep this tab active: CloudShell ends idle sessions after ~20 minutes, and EKS
takes about that long. If the session drops, open a new one and run the same
command — state is in S3, and the apply resumes.
EOF

  say "terraform apply"
  terraform -chdir="$TF_DIR" apply -input=true

  aws eks update-kubeconfig --name "$ENV_NAME" >/dev/null
  NUM_HISTORY_SHARDS="${NUM_HISTORY_SHARDS:-512}" bash "$ROOT/scripts/deploy.sh"
  bash "$ROOT/scripts/smoke-test.sh"
  cmd_where
}

cmd_deploy() {
  prepare
  cluster_exists || die "cluster $ENV_NAME does not exist — run: bash $0 up"
  refresh_allowlist
  bash "$ROOT/scripts/deploy.sh"
}

cmd_smoke() {
  prepare
  refresh_allowlist
  bash "$ROOT/scripts/smoke-test.sh"
}

cmd_connect() {
  prepare
  refresh_allowlist
  kubectl get nodes
}

cmd_status() {
  prepare
  cluster_exists || { echo "cluster $ENV_NAME does not exist"; return; }
  refresh_allowlist
  helm list -A
  kubectl -n temporal get pods
  cmd_where
}

cmd_where() {
  cat <<EOF

Temporal is in namespace 'temporal' on EKS cluster '$ENV_NAME' ($AWS_REGION).
State: s3://$STATE_BUCKET/$ENV_NAME/terraform.tfstate

CloudShell cannot open the Temporal UI or Grafana. To reach them from your
laptop, re-run with your laptop on the allowlist:
  EXTRA_ADMIN_CIDRS=<your-ip>/32 bash $0 connect
then, on the laptop:
  aws eks update-kubeconfig --region $AWS_REGION --name $ENV_NAME
  kubectl -n temporal   port-forward svc/temporal-web 8080:8080
  kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
EOF
}

cmd_down() {
  prepare
  if cluster_exists; then
    refresh_allowlist
  else
    warn "cluster not found; skipping the Kubernetes cleanup step"
  fi
  TF_DIR="$TF_DIR" bash "$ROOT/scripts/teardown.sh"
  cat <<EOF

The state bucket s3://$STATE_BUCKET is kept: it is shared by every environment
in this account and region, and it is the record of what was destroyed. It
costs cents. Delete it yourself if nothing else uses it.
EOF
}

case "${1:-}" in
  up)      cmd_up ;;
  deploy)  cmd_deploy ;;
  smoke)   cmd_smoke ;;
  status)  cmd_status ;;
  connect) cmd_connect ;;
  down)    cmd_down ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
