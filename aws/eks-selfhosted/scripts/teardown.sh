#!/usr/bin/env bash
# Tear the environment down, in the order that leaves nothing billable behind.
#
#   1. helm uninstall + delete namespaces — releases the Prometheus EBS volume.
#      Skip this and `terraform destroy` removes the cluster but the volume,
#      created by the CSI driver and unknown to Terraform, survives and bills.
#   2. turn off RDS deletion protection (it is on by default, deliberately)
#   3. terraform destroy
#
# What survives, on purpose, and costs money until you delete it:
#   - the RDS final snapshot ("<name>-final-<timestamp>")
#   - RDS automated backups (delete_automated_backups = false)
#   - both Secrets Manager secrets, for their 7-day recovery window
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="${TF_DIR:-$HERE/../terraform}"

name="$(terraform -chdir="$TF_DIR" output -raw cluster_name)"
region="$(terraform -chdir="$TF_DIR" output -raw region)"

cat <<EOF
This destroys EKS cluster '$name', its VPC, and its RDS database in $region.
A final RDS snapshot is kept. Type the cluster name to continue:
EOF
read -r answer
[ "$answer" = "$name" ] || { echo "aborted"; exit 1; }

# Re-runnable: if a previous teardown already removed the cluster, skip
# straight to Terraform rather than failing on kubeconfig.
if aws eks describe-cluster --region "$region" --name "$name" >/dev/null 2>&1; then
  aws eks update-kubeconfig --region "$region" --name "$name" >/dev/null

  echo "==> uninstalling Helm releases"
  helm -n temporal uninstall temporal --wait 2>/dev/null || true
  helm -n monitoring uninstall monitoring --wait 2>/dev/null || true

  echo "==> deleting namespaces (releases PVC-backed EBS volumes)"
  kubectl delete namespace temporal monitoring --ignore-not-found --wait=true --timeout=10m

  left="$(kubectl get pv -o name 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$left" != 0 ]; then
    echo "WARNING: $left PersistentVolume(s) still exist; their EBS volumes may outlive the cluster:"
    kubectl get pv
  fi
else
  echo "==> cluster $name not found; skipping Kubernetes cleanup"
fi

echo "==> disabling RDS deletion protection"
terraform -chdir="$TF_DIR" apply -auto-approve -var db_deletion_protection=false \
  -target=aws_db_instance.temporal

echo "==> terraform destroy"
terraform -chdir="$TF_DIR" destroy -auto-approve -var db_deletion_protection=false

echo
echo "Done. Still billing until you remove them: the final RDS snapshot and automated backups."
echo "  aws rds describe-db-snapshots --region $region --query \"DBSnapshots[?starts_with(DBSnapshotIdentifier,'$name-final')].DBSnapshotIdentifier\""
