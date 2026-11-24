#!/usr/bin/env bash
# Deploy a Coolify application at a given image tag and wait for the outcome.
#
# Environment:
#   VERSION          image tag written to the application's TAG variable
#   COOLIFY_API_URL  base URL of the Coolify instance
#   COOLIFY_TOKEN    API token (never printed)
#   APP_UUID         Coolify application UUID
#   TIMEOUT_SECONDS  maximum wait for each deployment attempt
#   DEPLOY_ATTEMPTS  how many deployments to trigger when Coolify reports one as failed
#   CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET
#                    optional Cloudflare Access service token (never printed); both or neither
#
# Exit status: 0 when Coolify reports a deployment finished, 1 when every attempt failed, or on
# cancel or timeout.
#
# A failed deployment is triggered again because the usual cause is an image pull cut off midway
# (registry connection reset or unexpected EOF on a large layer); layers already pulled stay on the
# server, so the next attempt resumes where the last one broke. Cancel and timeout are not retried:
# a person cancelled it, or the deployment may still be running.
#
# Coolify v4 API endpoints used: PATCH /applications/{uuid}/envs (write), POST /deploy (deploy),
# GET /deployments/{uuid} (read); the token needs all three permissions.
set -euo pipefail

: "${VERSION:?VERSION is required}"
: "${COOLIFY_API_URL:?COOLIFY_API_URL is required}"
: "${COOLIFY_TOKEN:?COOLIFY_TOKEN is required}"
: "${APP_UUID:?APP_UUID is required}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"
DEPLOY_ATTEMPTS="${DEPLOY_ATTEMPTS:-3}"
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
if ! [[ "$DEPLOY_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  log error "DEPLOY_ATTEMPTS must be a positive integer"
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

# deploy_once ATTEMPT: trigger one deployment and wait for it.
# Returns 0 when finished, 2 when it failed (worth retrying), 1 on cancel, timeout or API errors.
deploy_once() {
  local attempt=$1 response deployment status deadline
  log info "triggering deployment (attempt $attempt of $DEPLOY_ATTEMPTS)"
  # Coolify 4.7 answers GET /deploy with 405; POST accepts the same query parameters.
  if ! response=$(call --request POST "$api/deploy?uuid=$APP_UUID&force=false"); then
    log error "deploy request failed: ${response:-no response}"
    return 1
  fi
  deployment=$(jq -r '.deployments[0].deployment_uuid // empty' <<<"$response")
  if [ -z "$deployment" ]; then
    log error "no deployment uuid in response: $response"
    return 1
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
        return 0
        ;;
      failed)
        log warn "deployment $deployment failed; see its log in Coolify"
        return 2
        ;;
      cancelled*)
        log error "deployment $deployment ended with status $status"
        return 1
        ;;
    esac
    sleep "$POLL_SECONDS"
  done

  log error "deployment $deployment did not finish within ${TIMEOUT_SECONDS}s"
  return 1
}

for ((attempt = 1; attempt <= DEPLOY_ATTEMPTS; attempt++)); do
  result=0
  deploy_once "$attempt" || result=$?
  case "$result" in
    0) exit 0 ;;
    2) ;;
    *) exit 1 ;;
  esac
done

log error "all $DEPLOY_ATTEMPTS deployment attempts failed"
exit 1
