#!/usr/bin/env sh
# Prints the environment that points the test suite at the container from
# docker-compose.test.yml. Usage: eval "$(sh hack/test-env.sh)"
cat <<'VARS'
export BOOTH_TEST_POSTGRES_DSN=postgresql://booth:booth-test@127.0.0.1:15434/booth_pipeline_test
export BOOTH_TEST_NATS_URL=nats://127.0.0.1:14222
export BOOTH_TEST_NATS_AUTH_URL=nats://127.0.0.1:14223
VARS
