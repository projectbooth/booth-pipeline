#!/usr/bin/env bash
# Local dev/testing helper: a real Keycloak (booth-architecture/local-dev's `booth-local` realm)
# plus real access tokens for its test users — so this module can be exercised against a real
# identity provider (real signatures, real `groups` claim, real issuer) rather than only fakes.
#
#   hack/dev-keycloak.sh up        start Keycloak on :8081 and enable password grants (~30s)
#   hack/dev-keycloak.sh token bob print an access token for alice|bob|carol|dave
#   hack/dev-keycloak.sh down      remove it
#
# Nothing here changes booth-architecture: `start-dev` Keycloak is an ephemeral container, and the
# realm's public `booth-design` client has direct grants OFF (correct for the browser flow), so we
# switch them on for THIS throwaway instance through the admin API instead of forking the realm.
#
# Issuer: tokens are minted against $KC_HOST (default localhost:8081) and Keycloak derives `iss`
# from the request's host, so a module verifying them must be given that same issuer URL. From a
# container use KC_HOST=host.docker.internal:8081 for BOTH minting and the module's issuer.
set -euo pipefail

HUB="${BOOTH_ARCHITECTURE_DIR:-$(cd "$(dirname "$0")/../../booth-architecture" && pwd)}"
KC_HOST="${KC_HOST:-localhost:8081}"
KC="http://${KC_HOST}"
REALM="booth-local"
PASSWORD="booth-dev-password"

admin_token() {
  curl -sf "$KC/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli -d username=admin -d password=admin |
    python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

case "${1:-}" in
  up)
    docker compose -p booth-pipeline-keycloak -f "$HUB/local-dev/docker-compose.yml" up -d
    printf 'waiting for Keycloak'
    for _ in $(seq 1 90); do
      curl -sf "$KC/realms/$REALM/.well-known/openid-configuration" >/dev/null 2>&1 && break
      printf '.'; sleep 2
    done
    echo
    tok="$(admin_token)"
    cid="$(curl -sf -H "Authorization: Bearer $tok" "$KC/admin/realms/$REALM/clients?clientId=booth-design" |
      python -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
    rep="$(curl -sf -H "Authorization: Bearer $tok" "$KC/admin/realms/$REALM/clients/$cid" |
      python -c 'import json,sys; d=json.load(sys.stdin); d["directAccessGrantsEnabled"]=True; print(json.dumps(d))')"
    curl -sf -X PUT -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
      -d "$rep" "$KC/admin/realms/$REALM/clients/$cid"
    echo "Keycloak ready: issuer $KC/realms/$REALM (users alice=owner bob=editor carol=viewer dave=none, password $PASSWORD)"
    ;;
  token)
    curl -sf "$KC/realms/$REALM/protocol/openid-connect/token" \
      -d grant_type=password -d client_id=booth-design -d "username=${2:?user}" -d "password=$PASSWORD" |
      python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
    ;;
  down)
    docker compose -p booth-pipeline-keycloak -f "$HUB/local-dev/docker-compose.yml" down -v
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 2
    ;;
esac
