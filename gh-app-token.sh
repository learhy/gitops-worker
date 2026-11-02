#!/usr/bin/env bash
# Mint a GitHub App installation token for project-stdio-orchestrator.
# Usage: gh-app-token.sh            -> prints token to stdout
#        eval "$(gh-app-token.sh)"  -> exports GH_TOKEN and GITHUB_TOKEN
#
# Tokens are cached on disk and refreshed when <45 min of life remain.
# App: project-stdio-orchestrator (id 3728222), installation 132706546.
# Grants read/write on issues, PRs, refs, and contents for all installed repos.
set -euo pipefail
# GitHub API needs direct egress; the host's scraping-proxy env (decodo)
# breaks python urllib/curl to api.github.com when its listener is down.
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy
unset CURL_CA_BUNDLE SSL_CERT_FILE REQUESTS_CA_BUNDLE

APP_ID="3728222"
INST_ID="132706546"
PEM="$HOME/.config/studio/github-app.pem"
CACHE="$HOME/.cache/gh-app-token"
CACHE_META="$CACHE/meta.json"

mkdir -p "$CACHE"

fresh_enough() {
  # true if cached token has >45 min left
  [ -f "$CACHE_META" ] || return 1
  python3 - "$CACHE_META" <<'EOF'
import json, sys, time, os
try:
    meta = json.load(open(sys.argv[1]))
    expires = time.mktime(time.strptime(meta["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    if expires - time.time() > 45 * 60 and os.path.exists(meta["token_file"]):
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
EOF
}

mint() {
  python3 - "$APP_ID" "$INST_ID" "$PEM" "$CACHE" <<'EOF'
import json, sys, time, os, urllib.request
import jwt
from cryptography.hazmat.primitives import serialization

app_id, inst_id, pem_path, cache_dir = sys.argv[1:5]
key = serialization.load_pem_private_key(open(pem_path).read().encode(), password=None)
now = int(time.time())
tok = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, key, algorithm="RS256")

req = urllib.request.Request(
    f"https://api.github.com/app/installations/{inst_id}/access_tokens", method="POST")
req.add_header("Authorization", f"Bearer {tok}")
req.add_header("Accept", "application/vnd.github+json")
with urllib.request.urlopen(req, timeout=20) as r:
    d = json.load(r)

token_file = os.path.join(cache_dir, "token")
with open(token_file, "w") as f:
    f.write(d["token"])
os.chmod(token_file, 0o600)
with open(os.path.join(cache_dir, "meta.json"), "w") as f:
    json.dump({"expires_at": d["expires_at"], "token_file": token_file}, f)
print(d["token"], end="")
EOF
}

if fresh_enough; then
  TOKEN_FILE=$(python3 -c "import json;print(json.load(open('$CACHE_META'))['token_file'])")
  TOKEN=$(cat "$TOKEN_FILE")
else
  TOKEN=$(mint)
fi

# Caller may eval us to get env vars
if [ "${1:-}" = "--export" ]; then
  echo "export GH_TOKEN='$TOKEN'"
  echo "export GITHUB_TOKEN='$TOKEN'"
else
  echo "$TOKEN"
fi
