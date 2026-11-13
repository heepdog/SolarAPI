#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
env_file="${APPSYSTEMS_ENV_FILE:-${script_dir}/.env}"

if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

: "${APPSYSTEMS_APP_ID:?Set APPSYSTEMS_APP_ID to your OpenAPI App ID}"
: "${APPSYSTEMS_APP_SECRET:?Set APPSYSTEMS_APP_SECRET to your OpenAPI App Secret}"
: "${APPSYSTEMS_SYSTEM_ID:?Set APPSYSTEMS_SYSTEM_ID to your system ID}"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--check-system" ) ]]; then
  printf 'Usage: %s [--check-system]\n' "${BASH_SOURCE[0]}" >&2
  exit 2
fi

for command in curl date jq openssl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done

if [[ -n "${APPSYSTEMS_TIMEZONE:-}" ]]; then
  yesterday=$(TZ="$APPSYSTEMS_TIMEZONE" date -d yesterday +%F)
else
  yesterday=$(date -d yesterday +%F)
fi

base_url="${APPSYSTEMS_BASE_URL:-https://api.apsystemsema.com:9282}"
if [[ "${1:-}" == "--check-system" ]]; then
  request_path="/user/api/v2/systems/details/${APPSYSTEMS_SYSTEM_ID}"
else
  request_path="/user/api/v2/systems/energy/${APPSYSTEMS_SYSTEM_ID}"
fi
timestamp="$(date +%s)000"
nonce=$(openssl rand -hex 16)
signature_path="${request_path##*/}"
string_to_sign="${timestamp}/${nonce}/${APPSYSTEMS_APP_ID}/${signature_path}/GET/HmacSHA256"
signature=$(
  printf '%s' "$string_to_sign" |
    openssl dgst -sha256 -hmac "$APPSYSTEMS_APP_SECRET" -binary |
    openssl base64 -A
)

request_args=(
  --fail-with-body --silent --show-error --get
  "${base_url}${request_path}"
  -H "X-CA-AppId: ${APPSYSTEMS_APP_ID}"
  -H "X-CA-Timestamp: ${timestamp}"
  -H "X-CA-Nonce: ${nonce}"
  -H 'X-CA-Signature-Method: HmacSHA256'
  -H "X-CA-Signature: ${signature}"
)

if [[ "${1:-}" != "--check-system" ]]; then
  request_args+=(
    --data-urlencode 'energy_level=hourly'
    --data-urlencode "date_range=${yesterday}"
  )
fi

curl "${request_args[@]}" |
  if [[ "${1:-}" == "--check-system" ]]; then
    jq -e --arg sid "$APPSYSTEMS_SYSTEM_ID" '
      if .code != 0 then
        error("APsystems API returned code \(.code): \(tojson)")
      elif .data.sid != $sid then
        error("System-details response did not match the configured system ID")
      else
        "System-details request succeeded for the configured system ID."
      end
    '
  else
    jq -e --arg date "$yesterday" '
    if .code != 0 then
      error("APsystems API returned code \(.code): \(tojson)")
    elif (.data | type) != "array" then
      error("APsystems response data is not an array")
    elif (.data | length) != 24 then
      error("Expected 24 hourly values; got \(.data | length)")
    else
      {
        date: $date,
        energy_kwh: ([.data[] | tonumber] | add)
      }
    end
    '
  fi
