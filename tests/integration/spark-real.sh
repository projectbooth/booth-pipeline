#!/usr/bin/env bash
# Pipeline's Spark calls against the REAL booth-spark (its published chart and image, pinned below), so the
# stand-in (fixtures/spark/) can't drift from the real contract unnoticed. Gate 3 found exactly that:
# GET /v1/limits, which the stand-in answered, was never built in booth-spark 0.3.0.
#
# Real here: booth-spark's chart, backend image, token verification, /v1 routes, run controller, and a
# real Spark driver and executor (apache/spark, the digest booth-spark's own Integration pins). Stand-in:
# booth-core (fake-core-broker.yaml), which mints RS256 workload tokens booth-spark verifies against
# /.well-known/jwks.json, issues person tokens from a test identity provider, and proxies /modules/spark/*
# as core's gateway does (X-Workspace forwarded as X-Booth-Workspace). No data access: that needs
# booth-storage, booth-lakehouse and the runtime image, and is booth-spark's own `data` job and the
# homelab gate.
#
# What it checks (task_jobs_driver.py spark-real):
#   - person and workload tokens reach /v1 through the gateway path (GET /v1/me);
#   - pipeline's availability probe (GET /v1/limits as the caller): available when SPARK_HAS_LIMITS=1,
#     and then the stand-in's /v1/limits body has exactly the real one's shape; with SPARK_HAS_LIMITS=0
#     (0.3.0) the 404 is recorded as the known gap rather than passed silently;
#   - a scheduled run's Spark task through pipeline's own client: submit, states, the driver log through
#     the log cursor, the output line, the downstream task;
#   - booth-spark's own refusal (a conf key it doesn't allow) reaches the run as Spark's message;
#   - cancel: pipeline's stop reaches booth-spark, and the application ends `stopped`.
#
#   spark-real.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
ns=booth-pipeline
cluster=${KIND_CLUSTER:-booth-pipeline-ci}

# booth-spark 0.3.0 (release run of booth-spark@6df9cb3): chart and backend image, by digest. The amd64
# manifest is what kind loads (it can't import a multi-arch index); runs use plain apache/spark.
SPARK_CHART_VERSION=0.3.0
SPARK_CHART_DIGEST=sha256:d2dd414fa978bb8325477530f99ba5104d711c73a7550fff2aa29ca2dde4a3a2
SPARK_BACKEND=ghcr.io/projectbooth/booth-spark@sha256:90fa5973863b16af253e772ce1aa49d80b9d80e13e0e7ee046e77c4739f088d3
SPARK_RUNTIME=apache/spark@sha256:7cdb42ed55b5e02c279e838e38da3336dae74dca6aef07defae93f3c197d8ed6
# 0.3.0 has no GET /v1/limits (booth-spark adds it in 0.3.1). Set 1 when the pin moves to a version with it.
export SPARK_HAS_LIMITS=${SPARK_HAS_LIMITS:-0}
core=http://booth-core.booth-system.svc.cluster.local:8080

echo "=== booth-spark ${SPARK_CHART_VERSION}'s images, loaded into kind"
docker pull -q "$SPARK_BACKEND"
docker pull -q "$SPARK_RUNTIME"
kind load docker-image "$SPARK_BACKEND" --name "$cluster"
kind load docker-image "$SPARK_RUNTIME" --name "$cluster"

echo "=== booth-spark ${SPARK_CHART_VERSION}, from its published chart, beside its own PostgreSQL"
kubectl create namespace booth-spark --dry-run=client -o yaml | kubectl apply -f -
kubectl -n booth-spark apply -f "$here/fixtures/postgres.yaml"
kubectl -n booth-spark rollout status deployment/postgres --timeout=180s
kubectl -n booth-spark create secret generic booth-spark-db \
  --from-literal=dsn="postgresql://booth:booth-test@postgres.booth-spark.svc:5432/booth_pipeline?sslmode=disable" \
  --dry-run=client -o yaml | kubectl apply -f -
tmp=$(mktemp -d)
helm pull "oci://ghcr.io/projectbooth/charts/booth-spark@$SPARK_CHART_DIGEST" --destination "$tmp" >/dev/null
chart=$(ls "$tmp"/*.tgz)
tar -xzOf "$chart" booth-spark/Chart.yaml | grep -qx "version: $SPARK_CHART_VERSION" || { echo "the chart at $SPARK_CHART_DIGEST is not $SPARK_CHART_VERSION"; exit 1; }
helm upgrade --install booth-spark "$chart" --namespace booth-spark \
  --set image.repository="${SPARK_BACKEND%@*}" --set image.tag="${SPARK_CHART_VERSION}@${SPARK_BACKEND#*@}" --set image.pullPolicy=IfNotPresent \
  --set postgres.provisionedByCore=false --set postgres.dsnSecret.name=booth-spark-db \
  --set oidc.issuerUrl="$core/test-idp" --set oidc.clientId=booth-spark-ci --set oidc.requireAudience=false \
  --set oidc.workloadIssuerUrl="$core" \
  --set runs.image="$SPARK_RUNTIME" --set runs.imagePullPolicy=IfNotPresent \
  --wait --timeout 10m
kubectl -n booth-spark rollout status deploy/booth-spark --timeout=180s

echo "=== the stand-in gateway sends /modules/spark/* to the real booth-spark"
kubectl -n "$ns" exec deploy/booth-pipeline -- python -c 'import json,urllib.request,sys
r=urllib.request.Request(sys.argv[1]+"/_control/spark", data=json.dumps({"upstream":"http://booth-spark.booth-spark.svc:8080"}).encode(), method="POST")
print(urllib.request.urlopen(r, timeout=10).read().decode())' "$core"

echo "=== pipeline's Spark calls, against the real booth-spark"
status=0
kubectl -n "$ns" exec -i deploy/booth-pipeline -- env SPARK_HAS_LIMITS="$SPARK_HAS_LIMITS" python - spark-real <"$here/task_jobs_driver.py" || status=$?

echo "=== back to the stand-in for anything after this step"
kubectl -n "$ns" exec deploy/booth-pipeline -- python -c 'import json,urllib.request,sys
r=urllib.request.Request(sys.argv[1]+"/_control/spark", data=json.dumps({"upstream":None}).encode(), method="POST")
urllib.request.urlopen(r, timeout=10).read()' "$core"
[ "$status" = 0 ] || { kubectl -n booth-spark logs deploy/booth-spark --tail=100 || true; exit "$status"; }
echo
echo "spark-real: OK"
