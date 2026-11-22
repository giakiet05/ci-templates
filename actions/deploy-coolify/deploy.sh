#!/usr/bin/env bash
# Deploy a Coolify application at a given image tag and wait for the outcome.
#
# Environment:
#   VERSION          image tag written to the application's TAG variable
#   COOLIFY_API_URL  base URL of the Coolify instance
#   COOLIFY_TOKEN    API token (never printed)
#   APP_UUID         Coolify application UUID
#   TIMEOUT_SECONDS  maximum wait for the deployment
#   CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET
#                    optional Cloudflare Access service token (never printed); both or neither
#
# Exit status: 0 when Coolify reports the deployment finished, 1 on failure, cancel or timeout.
#
# Coolify v4 API endpoints used: PATCH /applications/{uuid}/envs, POST /deploy, GET /deployments/{uuid}.
set -euo pipefail

: "${VERSION:?VERSION is required}"
: "${COOLIFY_API_URL:?COOLIFY_API_URL is required}"
: "${COOLIFY_TOKEN:?COOLIFY_TOKEN is required}"
: "${APP_UUID:?APP_UUID is required}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
CF_ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}"
CF_ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}"
POLL_SECONDS=10

api="${COOLIFY_API_URL%/}/api/v1"

# log LEVEL MESSAGE: one structured line per event, never containing the token.
log() {
  printf '%s level=%s app=%s version=%s msg="%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$APP_UUID" "$VERSION" "$2"
}

# A half-configured service token would be rejected by Access with an HTML login page, which is
# harder to diagnose than failing here.
if { [ -n "$CF_ACCESS_CLIENT_ID" ] && [ -z "$CF_ACCESS_CLIENT_SECRET" ]; } ||
  { [ -z "$CF_ACCESS_CLIENT_ID" ] && [ -n "$CF_ACCESS_CLIENT_SECRET" ]; }; then
  log error "set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET, or neither"
  exit 1
fi

# Headers go through a curl config on stdin so the secrets never appear in the process list.
auth_config() {
  printf 'header = "Authorization: Bearer %s"\n' "$COOLIFY_TOKEN"
  if [ -n "$CF_ACCESS_CLIENT_ID" ]; then
    printf 'header = "CF-Access-Client-Id: %s"\n' "$CF_ACCESS_CLIENT_ID"
    printf 'header = "CF-Access-Client-Secret: %s"\n' "$CF_ACCESS_CLIENT_SECRET"
  fi
}

# call ARGS...: authenticated Coolify API request; fails on HTTP errors and prints the body.
call() {
  auth_config | curl --fail-with-body --silent --show-error \
    --config - \
    --header "Content-Type: application/json" \
    "$@"
}

log info "setting TAG"
payload=$(jq -nc --arg value "$VERSION" '{key: "TAG", value: $value}')
if ! response=$(call --request PATCH "$api/applications/$APP_UUID/envs" --data "$payload"); then
  log error "failed to set TAG: ${response:-no response}"
  exit 1
fi

log info "triggering deployment"
# Coolify 4.7 answers GET /deploy with 405; POST accepts the same query parameters.
if ! response=$(call --request POST "$api/deploy?uuid=$APP_UUID&force=false"); then
  log error "deploy request failed: ${response:-no response}"
  exit 1
fi
deployment=$(jq -r '.deployments[0].deployment_uuid // empty' <<<"$response")
if [ -z "$deployment" ]; then
  log error "no deployment uuid in response: $response"
  exit 1
fi
log info "waiting for deployment $deployment"

deadline=$((SECONDS + TIMEOUT_SECONDS))
while ((SECONDS < deadline)); do
  if ! response=$(call "$api/deployments/$deployment"); then
    log warn "status request failed, retrying: ${response:-no response}"
    sleep "$POLL_SECONDS"
    continue
  fi
  status=$(jq -r '.status // "unknown"' <<<"$response")
  case "$status" in
    finished)
      log info "deployment $deployment finished"
      exit 0
      ;;
    failed | cancelled*)
      log error "deployment $deployment ended with status $status"
      exit 1
      ;;
  esac
  sleep "$POLL_SECONDS"
done

log error "deployment $deployment did not finish within ${TIMEOUT_SECONDS}s"
exit 1
