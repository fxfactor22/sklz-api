#!/usr/bin/env bash
# Where does copying stop for each follower? Admin key never on the
# command line and never echoed:
#   read -rs "?Paste key: " SKLZ_ADMIN_KEY && export SKLZ_ADMIN_KEY
#   bash tools/copy_diag.sh
set -uo pipefail
: "${SKLZ_ADMIN_KEY:?set SKLZ_ADMIN_KEY first (read -rs, see header)}"
API="${SKLZ_API:-https://api.sklzlabs.com}"
body="$(mktemp)"
code="$(curl -sS -o "$body" -w '%{http_code}' \
        -H "Authorization: Bearer ${SKLZ_ADMIN_KEY}" "${API}/api/mt5copy/diag")"
echo "HTTP ${code}"
python3 -m json.tool "$body" 2>/dev/null || cat "$body"
echo
rm -f "$body"
