#!/usr/bin/env bash
set -euo pipefail

: "${KERKOV_IMAGE:=kerkov:ci}"
: "${ZAP_API_KEY:=kerkov-compose-smoke-key}"
export KERKOV_IMAGE ZAP_API_KEY

project_name="kerkov-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
compose=(docker compose --project-name "${project_name}")

cleanup() {
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" config --quiet
"${compose[@]}" up --detach --wait zap

# shellcheck disable=SC2016 # Expand these variables inside the Kerkov container.
"${compose[@]}" run --rm --no-deps --entrypoint /bin/bash kerkov -lc '
    set -euo pipefail

    [[ "$(id -u)" == "1000" ]]
    [[ "$(id -g)" == "1000" ]]
    [[ ! -e /var/run/docker.sock ]]

    cap_eff="$(awk '\''$1 == "CapEff:" {print $2}'\'' /proc/self/status)"
    [[ "${cap_eff}" =~ ^0+$ ]]
    grep --extended-regexp --quiet '\''^NoNewPrivs:[[:space:]]+1$'\'' /proc/self/status

    root_options="$(awk '\''$2 == "/" {print $4; exit}'\'' /proc/mounts)"
    [[ ",${root_options}," == *,ro,* ]]

    state_probe="$(mktemp /home/kerkov/.kerkov/.compose-smoke.XXXXXX)"
    rm -f "${state_probe}"

    zap_response="$(curl --fail --silent --show-error --retry 5 \
        "${ZAP_API_URL}/JSON/core/view/version/?apikey=${ZAP_API_KEY}")"
    [[ "${zap_response}" == *'\''"version"'\''* ]]
'

"${compose[@]}" run --rm --no-deps kerkov --help >/dev/null

echo "Compose smoke test passed for ${KERKOV_IMAGE}"
