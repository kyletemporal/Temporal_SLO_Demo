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
# (The RDS CloudWatch log group is Terraform-managed and IS destroyed.)
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

  # PV and EBS deletion is ASYNCHRONOUS after the namespace goes. Destroying
  # the cluster first removes the CSI driver that would delete the volume, and
  # the gp3 volume then outlives everything, still billing. Wait for it.
  echo "==> waiting for PersistentVolumes to be released (up to 5 min)"
  for _ in $(seq 1 60); do
    [ "$(kubectl get pv -o name 2>/dev/null | wc -l | tr -d ' ')" = 0 ] && break
    sleep 5
  done
  if [ "$(kubectl get pv -o name 2>/dev/null | wc -l | tr -d ' ')" != 0 ]; then
    kubectl get pv
    if [ "${FORCE_DESTROY:-0}" != 1 ]; then
      echo "ERROR: PersistentVolumes still exist. Destroying now would orphan their EBS volumes."
      echo "       Investigate (kubectl describe pv), or re-run with FORCE_DESTROY=1 and delete"
      echo "       the volumes yourself: aws ec2 describe-volumes --filters Name=tag:kubernetes.io/created-for/pv/name,Values=*"
      exit 1
    fi
    echo "WARNING: FORCE_DESTROY=1 — continuing; the volumes above will outlive the cluster."
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
