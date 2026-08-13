#!/usr/bin/env bash
#
# Point the minimum-standard rules at your Namespace and Task Queue.
#
#   ./scripts/configure.sh <namespace> <task-queue>
#   ./scripts/configure.sh my-team-prod orders
#
# Does the three things people get wrong by hand:
#   1. substitutes $NAMESPACE / $TASK_QUEUE portably (BSD sed and GNU sed
#      disagree about `sed -i`, and the naive form fails on macOS)
#   2. UNCOMMENTS AppWorkerFleetAbsent — the alert nothing else can replace,
#      and the step most likely to be skipped
#   3. validates the result with promtool if it is available
#
# Writes to ./configured/ and never edits the shipped templates, so you can
# re-run it or diff the output.

set -uo pipefail

NS="${1:-}"
TQ="${2:-}"
if [ -z "$NS" ] || [ -z "$TQ" ]; then
  echo "usage: $0 <namespace> <task-queue>"
  echo "   eg: $0 my-team-prod orders"
  exit 2
fi

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="configured"
mkdir -p "$OUT"

ok()   { printf "  \033[32mok\033[0m    %s\n" "$1"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$1"; }
die()  { printf "  \033[31mfail\033[0m  %s\n" "$1"; exit 1; }

printf "\n\033[1mConfiguring the minimum standard\033[0m\n"
printf "  namespace   %s\n  task queue  %s\n\n" "$NS" "$TQ"

# Substitution via python, not sed: `sed -i` needs a backup-suffix argument on
# BSD/macOS and rejects it on some GNU builds, and the difference is a silent
# source of "why did nothing change".
for f in prometheus/slo-rules.yml prometheus/alerts.yml; do
  [ -f "$f" ] || die "missing $f — run this from the app-team directory"
  NS="$NS" TQ="$TQ" SRC="$f" DST="$OUT/$(basename "$f")" python3 - <<'PY'
import os, pathlib
src, dst = os.environ["SRC"], os.environ["DST"]
s = pathlib.Path(src).read_text()
s = s.replace("$NAMESPACE", os.environ["NS"]).replace("$TASK_QUEUE", os.environ["TQ"])
pathlib.Path(dst).write_text(s)
PY
  ok "wrote $OUT/$(basename "$f")"
done

# Uncomment the absence alert. It ships disabled because absent() on an
# unsubstituted selector matches nothing and therefore fires immediately and
# permanently — but disabled is not where it should stay.
NS="$NS" TQ="$TQ" python3 - <<'PY'
import pathlib, re, sys
p = pathlib.Path("configured/alerts.yml")
s = p.read_text()
if "- alert: AppWorkerFleetAbsent" in s and not re.search(r"^\s*#\s*- alert: AppWorkerFleetAbsent", s, re.M):
    print("  ok    AppWorkerFleetAbsent already active"); sys.exit(0)
lines, out, inblock = s.split("\n"), [], False
for ln in lines:
    if re.match(r"^\s*#\s*- alert: AppWorkerFleetAbsent", ln):
        inblock = True
    elif inblock and ln.strip() and not ln.lstrip().startswith("#"):
        inblock = False
    if inblock and ln.lstrip().startswith("#"):
        out.append(re.sub(r"^(\s*)#\s?", r"\1", ln, count=1))
    else:
        out.append(ln)
p.write_text("\n".join(out))
print("  ok    AppWorkerFleetAbsent uncommented")
PY

if command -v promtool >/dev/null 2>&1; then
  if promtool check rules "$OUT"/*.yml >/dev/null 2>&1; then
    ok "promtool: both files valid"
  else
    promtool check rules "$OUT"/*.yml 2>&1 | tail -5
    die "promtool rejected the configured rules"
  fi
else
  warn "promtool not found — validate before loading:"
  echo "         promtool check rules $OUT/*.yml"
fi

# Guard against the trap that produces no series at all rather than a wrong
# number: the SLO boundaries must exist as histogram buckets, and `le` is a
# STRING match (le=\"1\" does not match the SDK's le=\"1.0\").
echo
grep -ho 'le="[^"]*"' "$OUT/slo-rules.yml" | sort -u | while read -r le; do
  echo "  note  SLO boundary $le — confirm it exists in your Worker's buckets:"
done
echo "        NAMESPACE=$NS TASK_QUEUE=$TQ ./scripts/conformance-check.sh"

cat <<EOF

Next:
  cp $OUT/*.yml /etc/prometheus/   # or wherever your rules live, then reload
  NAMESPACE=$NS TASK_QUEUE=$TQ ./scripts/conformance-check.sh
EOF
