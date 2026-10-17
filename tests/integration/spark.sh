#!/usr/bin/env bash
# The Spark runner (ADR 0119, ADR 0120; docs/design-spark-runner.md) on the real cluster: real task Jobs
# on Calico, the API pod's own service (store, engine, Job runner, Spark runner), the stand-in core's
# workload-token mint and a stand-in booth-spark behind its gateway (fixtures/spark/, served by the
# stand-in core) that runs the submitted PySpark code against a stub pyspark. Runs after the
# credential-sidecar steps, which deployed the stand-in core and set core.url.
#
# What it proves (task_jobs_driver.py spark-e2e):
#   - a scheduled run's Spark task submits, runs and succeeds as pipeline:acme:<runId> for its owner;
#     its output reaches the downstream task; its driver log and state are in the run log, and the
#     attempt records its application;
#   - a disabled service-account owner, and a refused person owner, fail the whole run at its start
#     with their messages, and nothing is submitted;
#   - cancel stops the application;
#   - a task pod killed mid-run: its application is stopped before the retry submits, and the retry
#     waits runner.spark.retryMinDelaySeconds.
# Not proved here: real Spark and its data access (booth-spark's own Integration, and the real-stack
# gate in the design note's section 11).
#
#   spark.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
ns=booth-pipeline

echo "=== the stand-in booth-spark, served by the stand-in core"
kubectl -n booth-system create configmap fake-spark \
  --from-file=spark_standin.py="$here/fixtures/spark/spark_standin.py" \
  --from-file=pyspark_init.py="$here/fixtures/spark/pyspark/__init__.py" \
  --from-file=pyspark_sql_init.py="$here/fixtures/spark/pyspark/sql/__init__.py" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n booth-system apply -f "$here/fixtures/fake-core-broker.yaml"
kubectl -n booth-system rollout restart deploy/booth-core
kubectl -n booth-system rollout status deploy/booth-core --timeout=180s

echo "=== runner.spark for the test: poll every second, a 10 s retry floor"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.spark.pollSeconds=1 --set runner.spark.retryMinDelaySeconds=10 \
  --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
for _ in $(seq 1 120); do  # the old API pod is gone, so exec reaches the new one
  [ "$(kubectl -n "$ns" get pod -l app.kubernetes.io/component=api -o name | wc -l)" = 1 ] && break
  sleep 1
done

echo "=== scheduled runs with a Spark task"
kubectl -n "$ns" exec -i deploy/booth-pipeline -- python - spark-e2e <"$here/task_jobs_driver.py"
echo
echo "spark: OK"
