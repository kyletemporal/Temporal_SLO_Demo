#!/usr/bin/env bash
#
# The CLI shim behind the self-hosted namespace module.
#
# There is NO Terraform provider for self-hosted Temporal namespaces — the
# temporalio/temporalcloud provider manages Temporal Cloud only. So this module
# drives the `temporal operator` CLI, and this script is the seam.
#
# Every subcommand is designed to be IDEMPOTENT, because Terraform will re-run
# them after any partial failure and a script that fails on "already exists" is
# worse than no automation at all.
#
# Subcommands:
#   read     data.external protocol — JSON query on stdin, flat JSON on stdout
#   create   create a namespace (no-op if it already exists)
#   update   update mutable settings (retention, description, email, data)
#   delete   delete a namespace, guarded by TF_ALLOW_DESTROY
#   attrs    ensure custom search attributes exist (additive only)

set -uo pipefail

# ---------------------------------------------------------------------------
# `temporal` must be on PATH. Fail with a usable message rather than a bare
# 127 buried in Terraform's provisioner output.
if ! command -v temporal >/dev/null 2>&1; then
  echo "temporal CLI not found on PATH. Install it: https://docs.temporal.io/cli" >&2
  exit 1
fi

# Duration -> seconds. The CLI reports retention as "345600s" but accepts "96h",
# so desired and actual are in different units and cannot be compared as
# strings. Normalising both to seconds is what makes drift detection possible.
to_seconds() {
  local d="${1:-}" n unit
  [ -z "$d" ] && { echo 0; return; }
  d="${d%s}"                       # a bare "345600s" from the API
  if [[ "$d" =~ ^[0-9]+$ ]]; then echo "$d"; return; fi
  local total=0
  while [[ "$d" =~ ^([0-9]+)([smhd])(.*)$ ]]; do
    n="${BASH_REMATCH[1]}"; unit="${BASH_REMATCH[2]}"; d="${BASH_REMATCH[3]}"
    case "$unit" in
      s) total=$((total + n)) ;;
      m) total=$((total + n * 60)) ;;
      h) total=$((total + n * 3600)) ;;
      d) total=$((total + n * 86400)) ;;
    esac
  done
  echo "$total"
}

# NAMESPACE CREATION IS ASYNCHRONOUS, AND THIS IS THE BUG THAT BIT FIRST.
#
# `temporal operator namespace create` returns success as soon as the record is
# written. The Frontend serves namespace lookups from a CACHE that refreshes on
# an interval (~10s by default), so for several seconds afterwards the
# namespace genuinely does not exist as far as every other API call is
# concerned. A follow-up `namespace update` in the same breath fails with:
#
#   Error: namespace update failed: Namespace orders is not found.
#
# Which reads like the create silently failed. It did not.
#
# Terraform makes this worse rather than better: it runs the fleet in parallel,
# so the gap between create and the next operation is milliseconds, not the
# seconds a human would naturally take.
wait_visible() {
  local name="$1" address="$2" deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if temporal operator namespace describe --namespace "$name" \
         --address "$address" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "namespace '$name' did not become visible within 60s of creation" >&2
  return 1
}

cmd="${1:-}"; shift || true

case "$cmd" in

# ---------------------------------------------------------------------------
# data.external: reads {"name":..., "address":...} from stdin and returns the
# LIVE state of the namespace. This is what gives the module real drift
# detection — without it a local-exec module is write-only and `plan` is a lie.
#
# A namespace that does not exist is NOT an error here. It returns
# exists="false" so the plan can show a create instead of blowing up.
read)
  query=$(cat)
  name=$(printf '%s' "$query"    | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
  address=$(printf '%s' "$query" | python3 -c 'import json,sys; print(json.load(sys.stdin)["address"])')

  if ! out=$(temporal operator namespace describe --namespace "$name" \
                --address "$address" -o json 2>/dev/null); then
    # Distinguish "no such namespace" from "cluster unreachable". Reporting a
    # down cluster as an absent namespace would make the next apply try to
    # CREATE every namespace you already have.
    if ! temporal operator cluster health --address "$address" >/dev/null 2>&1; then
      echo "cannot reach Temporal at $address — refusing to report namespaces as absent" >&2
      exit 1
    fi
    printf '{"exists":"false","retention_seconds":"0","description":"","owner_email":"","state":"","id":"","custom_attributes":""}\n'
    exit 0
  fi

  sa=$(temporal operator search-attribute list --namespace "$name" \
          --address "$address" -o json 2>/dev/null || echo '{}')

  printf '%s' "$out" | SA="$sa" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
info = d.get("namespaceInfo", {}) or {}
cfg  = d.get("config", {}) or {}

ttl = str(cfg.get("workflowExecutionRetentionTtl", "0"))
ttl = ttl[:-1] if ttl.endswith("s") else ttl
try: ttl = str(int(float(ttl)))
except ValueError: ttl = "0"

# Custom search attributes only. System ones are always present and are not
# ours to manage, so including them would show permanent drift.
try:
    custom = json.loads(os.environ.get("SA") or "{}").get("customAttributes") or {}
except Exception:
    custom = {}
# INDEXED_VALUE_TYPE_KEYWORD -> Keyword, so it compares against what a user typed.
def short(t):
    t = t.replace("INDEXED_VALUE_TYPE_", "").lower()
    return {"keyword_list": "KeywordList", "datetime": "Datetime"}.get(t, t.capitalize())
attrs = ",".join(f"{k}={short(v)}" for k, v in sorted(custom.items()))

json.dump({
    "exists": "true",
    "retention_seconds": ttl,
    "description": info.get("description") or "",
    "owner_email": info.get("ownerEmail") or "",
    "state": info.get("state") or "",
    "id": info.get("id") or "",
    "custom_attributes": attrs,
}, sys.stdout)
'
  ;;

# ---------------------------------------------------------------------------
# Idempotent create. Terraform re-runs provisioners after a partial apply, and
# a namespace that already exists must not fail the run.
create)
  : "${TF_NAME:?}" "${TF_ADDRESS:?}" "${TF_RETENTION:?}"
  if temporal operator namespace describe --namespace "$TF_NAME" \
       --address "$TF_ADDRESS" >/dev/null 2>&1; then
    echo "namespace '$TF_NAME' already exists — adopting it"
    exit 0
  fi
  set -- operator namespace create --namespace "$TF_NAME" --retention "$TF_RETENTION"
  [ -n "${TF_DESCRIPTION:-}" ] && set -- "$@" --description "$TF_DESCRIPTION"
  [ -n "${TF_EMAIL:-}" ]       && set -- "$@" --email "$TF_EMAIL"
  # shellcheck disable=SC2086
  for kv in ${TF_DATA:-}; do set -- "$@" --data "$kv"; done
  temporal "$@" --address "$TF_ADDRESS" || exit 1
  # Do not return until the namespace is actually resolvable, or every
  # dependent resource in the same apply races the cache and fails.
  wait_visible "$TF_NAME" "$TF_ADDRESS" || exit 1
  echo "created namespace '$TF_NAME' (retention $TF_RETENTION)"
  ;;

# ---------------------------------------------------------------------------
update)
  : "${TF_NAME:?}" "${TF_ADDRESS:?}"
  # Belt and braces: depends_on orders this after create, but Terraform's
  # dependency edge does not know about the server's namespace cache.
  wait_visible "$TF_NAME" "$TF_ADDRESS" || exit 1
  set -- operator namespace update --namespace "$TF_NAME"
  [ -n "${TF_RETENTION:-}" ]   && set -- "$@" --retention "$TF_RETENTION"
  [ -n "${TF_DESCRIPTION:-}" ] && set -- "$@" --description "$TF_DESCRIPTION"
  [ -n "${TF_EMAIL:-}" ]       && set -- "$@" --email "$TF_EMAIL"
  # shellcheck disable=SC2086
  for kv in ${TF_DATA:-}; do set -- "$@" --data "$kv"; done
  temporal "$@" --address "$TF_ADDRESS" || exit 1
  echo "updated namespace '$TF_NAME'"
  ;;

# ---------------------------------------------------------------------------
# Search attributes are ADDITIVE ONLY, and that is a server limitation, not a
# shortcut here: the CLI's own help says to contact support to remove one. So
# this ensures the attributes you declared exist, and never removes any.
# Removing one from your tfvars is therefore silent — see the module README.
attrs)
  : "${TF_NAME:?}" "${TF_ADDRESS:?}"
  [ -z "${TF_ATTRS:-}" ] && exit 0
  wait_visible "$TF_NAME" "$TF_ADDRESS" || exit 1
  existing=$(temporal operator search-attribute list --namespace "$TF_NAME" \
               --address "$TF_ADDRESS" -o json 2>/dev/null \
             | python3 -c 'import json,sys
try: print(" ".join((json.load(sys.stdin).get("customAttributes") or {}).keys()))
except Exception: print("")' 2>/dev/null || echo "")
  rc=0
  for pair in $TF_ATTRS; do
    n="${pair%%=*}"; t="${pair#*=}"
    case " $existing " in
      *" $n "*) echo "search attribute '$n' already present"; continue ;;
    esac
    if temporal operator search-attribute create --namespace "$TF_NAME" \
         --name "$n" --type "$t" --address "$TF_ADDRESS" >/dev/null 2>&1; then
      echo "created search attribute $n=$t"
    else
      echo "FAILED to create search attribute $n=$t" >&2; rc=1
    fi
  done
  exit $rc
  ;;

# ---------------------------------------------------------------------------
# Deleting a namespace deletes every Workflow history in it, irreversibly.
#
# `prevent_destroy` cannot help here: it only accepts a literal, so it cannot be
# driven per-namespace from a map. This runtime guard is the substitute — it
# FAILS rather than silently skipping, because a destroy that quietly leaves the
# namespace running would drop it from state and orphan it, which is the worst
# of both outcomes.
delete)
  : "${TF_NAME:?}" "${TF_ADDRESS:?}"
  if [ "${TF_ALLOW_DESTROY:-false}" != "true" ]; then
    cat >&2 <<EOF

  REFUSING to delete namespace '$TF_NAME'.

  This would destroy every Workflow history in it, and there is no undo.
  If you mean it, set allow_destroy = true for this namespace, apply that
  change on its own, and then destroy.

EOF
    exit 1
  fi
  temporal operator namespace delete --namespace "$TF_NAME" \
    --address "$TF_ADDRESS" --yes || exit 1
  echo "deleted namespace '$TF_NAME'"
  ;;

*)
  echo "usage: nsctl.sh {read|create|update|delete|attrs}" >&2
  exit 2
  ;;
esac
