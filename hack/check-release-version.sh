#!/bin/sh
# Fail unless every version in the repo is the one being released:
#   sh hack/check-release-version.sh v0.4.0 [repo root]
# The release workflow runs this first. It packages the chart with --version from the tag either way, so
# without this check a stale pyproject or __version__ ships silently, and /healthz reports the old version.
set -eu

want="${1:?usage: check-release-version.sh <tag> [repo root]}"
want="${want#v}"
root="${2:-$(dirname "$0")/..}"

read_version() { # read_version <name> <sed expression> <file>
  got="$(sed -n "$2" "$root/$3" | tr -d '\r' | head -n 1)"
  if [ "$got" != "$want" ]; then
    echo "$1 ($3) says '$got', but the tag says '$want'" >&2
    failed=1
  fi
}

failed=0
read_version "project version" 's/^version = "\(.*\)".*$/\1/p' pyproject.toml
read_version "__version__" 's/^__version__ = "\(.*\)".*$/\1/p' src/booth_pipeline/__init__.py
read_version "chart version" 's/^version: \(.*\)$/\1/p' charts/booth-pipeline/Chart.yaml
read_version "chart appVersion" 's/^appVersion: "\(.*\)".*$/\1/p' charts/booth-pipeline/Chart.yaml

if [ "$failed" -ne 0 ]; then
  echo "bump them in one commit, then tag that commit" >&2
  exit 1
fi
echo "all versions are $want"
