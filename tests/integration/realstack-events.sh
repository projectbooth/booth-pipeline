#!/usr/bin/env bash
# Event triggers (ADR 0123) against the REAL publishers, on a throwaway kind cluster: Keycloak, booth-core
# (its authenticated NATS with JetStream, its gateway, broker and workload-token mint), booth-database,
# booth-catalog, booth-api (publishing api.write.committed through its outbox), booth-storage (publishing
# storage.object.created) and this booth-pipeline, each built from source and installed from its own
# chart. Every module's bus credential is core's, minted from its manifest's `events` (ADR 0050): nothing
# here grants itself anything. Then tests/integration/realstack/events_check.py runs as a Job in the
# cluster, through core's gateway only. See its header for what it checks.
#
#   CORE_DIR=… DATABASE_DIR=… CATALOG_DIR=… API_DIR=… STORAGE_DIR=… bash tests/integration/realstack-events.sh
#
# Each *_DIR is a checkout at the commit under test (the report names them). The Keycloak deployment and
# realm are booth-api's real-stack ones (API_DIR/test/integration/realstack), so the people and their
# workspace roles are the same: alice is an acme owner. Install values follow booth-api's
# hack/realstack-integration.sh. KEEP_CLUSTER=1 leaves the cluster up; CHECK_ONLY=1 then rebuilds and
# redeploys only booth-pipeline on that cluster and reruns the check (the rest stays as installed).
set -euo pipefail

CLUSTER=${CLUSTER:-booth-pipeline-events}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
for v in CORE_DIR DATABASE_DIR CATALOG_DIR API_DIR STORAGE_DIR; do
  d=${!v:-}
  [ -n "$d" ] && [ -d "$d" ] || { echo "$v must name a checkout"; exit 1; }
done
KC="$API_DIR/test/integration/realstack"
ISSUER=http://keycloak.keycloak.svc:8080/realms/booth
CORE_URL=http://booth-core.booth-system.svc:8080

cleanup() { [ "${KEEP_CLUSTER:-}" = 1 ] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
dump() {
  echo "=== state on failure"
  kubectl get pods -A -o wide || true
  kubectl get boothmodules -A -o custom-columns=NS:.metadata.namespace,ID:.spec.id,PUBLISH:.spec.events.publish,SUBSCRIBE:.spec.events.subscribe || true
  for d in booth-system/booth-core booth-api/a-booth-api booth-storage/storage-booth-storage booth-pipeline/booth-pipeline; do
    echo "--- logs $d"; kubectl -n "${d%%/*}" logs "deploy/${d##*/}" --tail=120 || true
  done
  kubectl -n events-check logs job/check --tail=300 || true
}

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --wait 120s

if [ "${CHECK_ONLY:-}" = 1 ]; then
  echo "--- CHECK_ONLY: booth-pipeline rebuilt and redeployed; everything else as installed"
  docker build -q -t booth-pipeline:rs "$REPO" >/dev/null
  kind load docker-image booth-pipeline:rs --name "$CLUSTER"
  helm upgrade booth-pipeline "$REPO/charts/booth-pipeline" -n booth-pipeline --reuse-values --wait --timeout 10m >/dev/null
  kubectl -n booth-pipeline rollout restart deploy/booth-pipeline
  kubectl -n booth-pipeline rollout status deploy/booth-pipeline --timeout=300s
  PASSWORD=$(kubectl -n events-check get configmap events-check-env -o jsonpath='{.data.password}')
else
echo "--- images"
helm repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
helm dependency build "$CORE_DIR/charts/booth-core" >/dev/null
docker build -q -t booth-core:rs "$CORE_DIR" >/dev/null
docker build -q -t booth-database:rs "$DATABASE_DIR" >/dev/null
docker build -q -t booth-catalog:rs "$CATALOG_DIR" >/dev/null
docker build -q -t booth-api:rs "$API_DIR" >/dev/null
docker build -q -t booth-storage:rs "$STORAGE_DIR" >/dev/null
docker build -q -t booth-pipeline:rs "$REPO" >/dev/null
kind load docker-image booth-core:rs booth-database:rs booth-catalog:rs booth-api:rs booth-storage:rs booth-pipeline:rs --name "$CLUSTER"

trap 'dump' ERR

echo "--- Keycloak (booth-api's real-stack realm: alice is an acme owner)"
PASSWORD=$(openssl rand -hex 12)
ADMIN_PASSWORD=$(openssl rand -hex 12)
kubectl create namespace keycloak --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n keycloak create secret generic keycloak-admin --from-literal=password="$ADMIN_PASSWORD" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
realm=$(mktemp)
sed "s/__TEST_PASSWORD__/$PASSWORD/g" "$KC/realm-booth.json.tpl" > "$realm"
kubectl -n keycloak create configmap keycloak-realm --from-file=realm-booth.json="$realm" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
rm -f "$realm"
kubectl apply -f "$KC/keycloak.yaml" >/dev/null
kubectl -n keycloak rollout status deploy/keycloak --timeout=600s

echo "--- booth-core (its NATS, JetStream stream BOOTH_EVENTS, per-module bus credentials)"
kubectl create namespace booth-system --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -f "$CORE_DIR/charts/booth-core/crds/" >/dev/null
helm upgrade --install booth-core "$CORE_DIR/charts/booth-core" -n booth-system \
  --set keycloak.enabled=false --set ingress.enabled=false \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design \
  --set workloadIdentity.issuerUrl=$CORE_URL \
  --set-string iframeSigningKey="$(openssl rand -hex 32)" \
  --set image.repository=booth-core --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null
kubectl -n booth-system rollout status deploy/booth-core --timeout=300s

echo "--- booth-database"
kubectl create namespace booth-database --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade --install db "$DATABASE_DIR/charts/booth-database" -n booth-database \
  --set tableEvents.interval=10s \
  --set image.repository=booth-database --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null

echo "--- booth-catalog (its own small PostgreSQL)"
kubectl create namespace booth-catalog --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CATPW=$(openssl rand -hex 12)
kubectl -n booth-catalog apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: pg, labels: {app: catalog-pg}}
spec:
  containers:
    - name: postgres
      image: postgres:16-alpine
      env:
        - {name: POSTGRES_USER, value: catalog}
        - {name: POSTGRES_PASSWORD, value: "$CATPW"}
        - {name: POSTGRES_DB, value: catalog}
      readinessProbe: {exec: {command: ["pg_isready", "-U", "catalog"]}, periodSeconds: 2}
---
apiVersion: v1
kind: Service
metadata: {name: catalog-pg}
spec: {selector: {app: catalog-pg}, ports: [{port: 5432}]}
EOF
kubectl -n booth-catalog create secret generic catalog-db \
  --from-literal=dsn="postgres://catalog:$CATPW@catalog-pg.booth-catalog.svc:5432/catalog?sslmode=disable" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n booth-catalog wait --for=condition=Ready pod/pg --timeout=180s >/dev/null
helm upgrade --install booth-catalog "$CATALOG_DIR/charts/booth-catalog" -n booth-catalog \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set oidc.groupsClaim=groups \
  --set nats.url=nats://booth-core-nats.booth-system.svc:4222 --set postgres.dsnSecret.name=catalog-db \
  --set image.repository=booth-catalog --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null

echo "--- booth-api (publishes api.write.committed, credential from core)"
kubectl create namespace booth-api --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade --install a "$API_DIR/charts/booth-api" -n booth-api \
  --set core.url=$CORE_URL --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design \
  --set oidc.workloadIssuerUrl=$CORE_URL \
  --set image.repository=booth-api --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null

echo "--- booth-storage (publishes storage.object.created, credential from core; a filesystem root per workspace)"
kubectl create namespace booth-storage --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade --install storage "$STORAGE_DIR/charts/booth-storage" -n booth-storage \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set oidc.workloadIssuerUrl=$CORE_URL \
  --set-json 'filesystem.roots=["/data/{workspace}"]' \
  --set-json 'filesystem.volumes=[{"name":"data","emptyDir":{}}]' \
  --set-json 'filesystem.volumeMounts=[{"name":"data","mountPath":"/data"}]' \
  --set image.repository=booth-storage --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null

echo "--- booth-pipeline (this branch; subscribes to both, credential from core)"
kubectl create namespace booth-pipeline --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade --install booth-pipeline "$REPO/charts/booth-pipeline" -n booth-pipeline \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set core.url=$CORE_URL \
  --set image.repository=booth-pipeline --set image.tag=rs --set image.pullPolicy=Never --wait --timeout 10m >/dev/null
kubectl get boothmodule -A -o custom-columns=NS:.metadata.namespace,ID:.spec.id,PUBLISH:.spec.events.publish,SUBSCRIBE:.spec.events.subscribe
for ns in booth-api booth-storage booth-pipeline; do
  kubectl -n "$ns" get secret booth-event-bus-credentials >/dev/null || { echo "core gave $ns no bus credential"; exit 1; }
done
echo "BOOTH_EVENTS duplicate window: $(kubectl -n booth-pipeline exec deploy/booth-pipeline -- python -c '
import asyncio, nats
async def go():
    nc = await nats.connect(open("/etc/booth/event-bus/url").read().strip(), user_credentials="/etc/booth/event-bus/nats.creds")
    s = await nc.jetstream().stream_info("BOOTH_EVENTS")
    print(f"{s.config.duplicate_window}s, max_age {s.config.max_age}s")
    await nc.close()
asyncio.run(go())')"

fi

echo "--- check (a Job, as alice and as an app, through core's gateway)"
kubectl create namespace events-check --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace events-check booth.projectbooth.io/database-client=true --overwrite >/dev/null
# A stand-in for booth-streamlit (id streamlit, workloadIdentity.mint): core delivers its minting credential
# here, so the check mints a real app token (streamlit:acme:budget) for booth-api's app write route.
kubectl apply -f - >/dev/null <<EOF
apiVersion: booth.projectbooth.io/v1alpha1
kind: BoothModule
metadata: {name: streamlit, namespace: events-check}
spec:
  id: streamlit
  displayName: Streamlit (events stand-in)
  version: 0.0.0
  contractVersion: "0.1.0"
  hasOwnUi: false
  healthCheckPath: /healthz
  workloadIdentity: {mint: true}
  serviceRef: {name: streamlit-stand-in, port: 8080}
EOF
for _ in $(seq 60); do kubectl -n events-check get secret booth-workload-minting-credentials >/dev/null 2>&1 && break; sleep 2; done
kubectl -n events-check create configmap events-check-env --from-literal=password="$PASSWORD" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n events-check create configmap events-check --from-file=events_check.py="$REPO/tests/integration/realstack/events_check.py" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n events-check delete job check --ignore-not-found >/dev/null
kubectl -n events-check apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata: {name: check}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: check
          image: booth-pipeline:rs
          imagePullPolicy: Never
          command: ["python", "-u", "/check/events_check.py"]
          env:
            - {name: TEST_PASSWORD, value: "$PASSWORD"}
            - name: STREAMLIT_MINT_URL
              valueFrom: {secretKeyRef: {name: booth-workload-minting-credentials, key: url}}
            - name: STREAMLIT_MINT_CREDENTIAL
              valueFrom: {secretKeyRef: {name: booth-workload-minting-credentials, key: credential}}
          volumeMounts:
            - {name: check, mountPath: /check}
      volumes:
        - name: check
          configMap: {name: events-check}
EOF
if ! kubectl -n events-check wait --for=condition=complete job/check --timeout=1200s; then
  kubectl -n events-check logs job/check || true
  dump
  exit 1
fi
kubectl -n events-check logs job/check
echo "--- the publishers and the consumer, from their own logs"
kubectl -n booth-storage logs deploy/storage-booth-storage | grep -i -E "event bus|eventbus|nats" | head -10 || true
kubectl -n booth-api logs deploy/a-booth-api | grep -i -E "event bus|outbox" | head -10 || true
kubectl -n booth-pipeline logs deploy/booth-pipeline | grep -E "event triggers" | head -10 || true
echo "realstack events: OK"
