#!/bin/sh
set -eu

# Render injects PORT; cobalt expects API_PORT.
export API_PORT="${PORT:-10000}"
: "${API_URL:?Set API_URL to this service's public HTTPS URL}"
: "${COBALT_PRIVATE_API_KEY:?Set a private UUIDv4 key in Render Environment}"

# Do not expose an unrestricted public API. This file is ephemeral and private.
umask 077
printf '{"%s":{"name":"darkcut-ai","limit":20,"allowedServices":["youtube"]}}\n' \
  "$COBALT_PRIVATE_API_KEY" > /tmp/darkcut-cobalt-keys.json
export API_KEY_URL="file:///tmp/darkcut-cobalt-keys.json"
export API_AUTH_REQUIRED=1
exec node src/cobalt