#!/usr/bin/env sh
set -eu

# Bake-time cloning of add-on repositories into /app/frame_tv_art_collections
# Expects ADDONS env/ARG listing repo names (comma/space/newline separated)
# Default clone URL pattern: https://github.com/kohlerryan/<name>.git
# Notes:
#  - If a name contains a slash (owner/repo), it is used verbatim as the repo path.
#  - Clone failures are non-fatal (continue building with available repos)
#  - Destination folder uses the literal add-on name (after stripping owner/".git"); this becomes artwork_dir

COL_DIR="/app/frame_tv_art_collections"
ADDONS_LIST="${ADDONS:-}"
DEFAULT_OWNER="kohlerryan"
# Also support BuildKit secret mounted at /run/secrets/github_token
if [ -z "${GITHUB_TOKEN:-}" ] && [ -f /run/secrets/github_token ]; then
  # Trim CR/LF and surrounding whitespace which can break http.extraHeader
  GITHUB_TOKEN="$(tr -d '\r\n' < /run/secrets/github_token | sed 's/^\s\+//; s/\s\+$//' || true)"
fi

# Prepare git clone command (with Authorization header when token present)
if [ -n "${GITHUB_TOKEN:-}" ] && [ ${#GITHUB_TOKEN} -gt 10 ]; then
  echo "bake_addons.sh: Using GitHub token (len=${#GITHUB_TOKEN}) for authenticated clones"
  # Optional: verbose curl for debugging if SAMSUNG_TV_ART_DEBUG_CLONE=true
  if [ "${SAMSUNG_TV_ART_DEBUG_CLONE:-false}" = "true" ]; then export GIT_CURL_VERBOSE=1; fi
  BASIC_B64=$(python - <<'PY'
import base64,os,sys
tok=os.environ.get('GITHUB_TOKEN','')
sys.stdout.write(base64.b64encode(('x-access-token:'+tok).encode()).decode())
PY
)
  GIT_CLONE() { git -c http.extraHeader="Authorization: Basic ${BASIC_B64}" clone --depth 1 "$@"; }
else
  echo "bake_addons.sh: No GitHub token provided; attempting unauthenticated clones"
  GIT_CLONE() { git clone --depth 1 "$@"; }
fi

if [ -z "$ADDONS_LIST" ]; then
  echo "bake_addons.sh: No ADDONS provided; nothing to clone."
  exit 0
fi

mkdir -p "$COL_DIR"

# Normalize commas/newlines to spaces
ADDONS_FLAT=$(printf "%s" "$ADDONS_LIST" | tr '\n' ' ' | sed 's/,/ /g')

trim() { printf "%s" "$1" | sed 's/^\s\+//; s/\s\+$//' ; }

# Avoid unexpected proxy interference inside build environment
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY no_proxy || true

for raw in $ADDONS_FLAT; do
  name=$(trim "$raw")
  [ -z "$name" ] && continue

  # Accept full URLs or SSH; otherwise construct default owner/repo
  case "$name" in
    http://*|https://*|git@*)
      url="$name"
      # Derive base_name from URL path
      path_part=${name##*:}
      path_part=${path_part#https://github.com/}
      path_part=${path_part#http://github.com/}
      base_name=${path_part##*/}
      ;;
    *)
      repo_path="$DEFAULT_OWNER/$name"
      base_name=${repo_path##*/}
      url="https://github.com/${repo_path%.git}.git"
      ;;
  esac
  case "$base_name" in
    *.git) base_name=${base_name%.git} ;;
  esac
  dest="$COL_DIR/$base_name"

  if [ -d "$dest/.git" ] || [ -d "$dest" ]; then
    echo "bake_addons.sh: Skipping existing $base_name"
    continue
  fi

  echo "bake_addons.sh: Cloning $url -> $dest"
  if ! GIT_CLONE "$url" "$dest"; then
    echo "bake_addons.sh: Warning: clone failed for $url (continuing)" >&2
    mkdir -p "$dest"
  fi
done

echo "bake_addons.sh: Completed cloning add-ons."
