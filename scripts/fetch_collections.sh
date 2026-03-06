#!/usr/bin/env sh
set -eu

# Fetch and update collection git repositories into the collections directory.
# Sources can be provided via:
#   - The environment variable SAMSUNG_TV_ART_COLLECTIONS as a space- or comma-separated
#     list of git URLs (optional suffix #branch). Note: .env files do not support multiline
#     values, so all URLs must be on a single line separated by spaces or commas.
#   - A newline-separated list of URLs in /data/collections.list (recommended for many repos).

COL_DIR="${SAMSUNG_TV_ART_COLLECTIONS_DIR:-/app/frame_tv_art_collections}"
LIST_FILE="/data/collections.list"
COL_LIST="${SAMSUNG_TV_ART_COLLECTIONS:-}"

# Optional GitHub token for private repositories
if [ -z "${GITHUB_TOKEN:-}" ] && [ -n "${SAMSUNG_TV_ART_GITHUB_TOKEN:-}" ]; then
  # Trim CR/LF and whitespace from env-provided token
  GITHUB_TOKEN="$(printf "%s" "${SAMSUNG_TV_ART_GITHUB_TOKEN}" | tr -d '\r\n' | sed 's/^\s\+//; s/\s\+$//' )"
fi
if [ -z "${GITHUB_TOKEN:-}" ] && [ -f /run/secrets/github_token ]; then
  GITHUB_TOKEN="$(tr -d '\r\n' < /run/secrets/github_token | sed 's/^\s\+//; s/\s\+$//' || true)"
fi

if [ -n "${GITHUB_TOKEN:-}" ] && [ ${#GITHUB_TOKEN} -gt 10 ]; then
  echo "fetch_collections.sh: Using GitHub token (len=${#GITHUB_TOKEN}) for authenticated clones"
  # Helper to URL-encode the token for safe inclusion in https://user:pass@ URLs
  urlencode() {
    # POSIX-safe URL encoder for password component
    local s="$1" out="" c i hex
    i=0
    while [ $i -lt ${#s} ]; do
      c=$(printf "%s" "$s" | cut -c $((i+1)))
      case "$c" in
        [a-zA-Z0-9.~_-]) out="$out$c" ;;
        *) hex=$(printf '%%%02X' "'"$c); out="$out$hex" ;;
      esac
      i=$((i+1))
    done
    printf "%s" "$out"
  }
  GIT_CLONE() { git -c credential.helper= -c http.sslVersion=tlsv1.2 clone --depth 1 "$@"; }
  GIT_FETCH() { git -c credential.helper= -c http.sslVersion=tlsv1.2 fetch --depth=1 "$@"; }
else
  echo "fetch_collections.sh: No GitHub token detected; attempting unauthenticated clones"
  GIT_CLONE() { git -c http.sslVersion=tlsv1.2 clone --depth 1 "$@"; }
  GIT_FETCH() { git -c http.sslVersion=tlsv1.2 fetch --depth=1 "$@"; }
fi

if [ -z "$COL_LIST" ] && [ -f "$LIST_FILE" ]; then
  # read newline-separated file into space-separated list
  COL_LIST=$(tr '\n' ' ' < "$LIST_FILE" || true)
fi

if [ -z "$COL_LIST" ]; then
  echo "No collections defined (SAMSUNG_TV_ART_COLLECTIONS or $LIST_FILE). Skipping fetch." >&2
  exit 0
fi

mkdir -p "$COL_DIR"

# Normalize commas into spaces and iterate
for src in $(echo "$COL_LIST" | sed 's/,/ /g'); do
  repo="$src"
  branch=""
  case "$repo" in
    *#*)
      branch="${repo#*#}"
      repo="${repo%%#*}"
      ;;
  esac

  name=$(basename "$repo" .git)
  dest="$COL_DIR/$name"

  if [ -d "$dest/.git" ]; then
    echo "Updating collection $name from $repo"
    (cd "$dest" && 
       GIT_FETCH origin ${branch:+$branch} >/dev/null 2>&1 || true && 
       if [ -n "$branch" ]; then git reset --hard origin/$branch >/dev/null 2>&1 || true; else git reset --hard FETCH_HEAD >/dev/null 2>&1 || true; fi
    ) || echo "Warning: failed to update $name"
  else
    echo "Cloning collection $name from $repo"
    # If token present and https, inject Basic creds in URL (avoid printing credentials)
    use_url="$repo"
    if [ -n "${GITHUB_TOKEN:-}" ] && printf "%s" "$repo" | grep -qE '^https://github\.com/'; then
      enc_tok=$(urlencode "$GITHUB_TOKEN")
      # Use 'oauth2' as username and token as password; GitHub accepts any username with a PAT as password
      use_url="https://oauth2:${enc_tok}@${repo#https://}"
    fi
    if [ -n "$branch" ]; then
      GIT_CLONE --branch "$branch" "$use_url" "$dest" || { echo "Clone failed for $repo"; continue; }
    else
      GIT_CLONE "$use_url" "$dest" || { echo "Clone failed for $repo"; continue; }
    fi
  fi
done

echo "Collection fetch complete."

# Prune directories that were previously cloned but are no longer in the list.
# Only removes subdirectories that contain a .git folder (i.e. were managed by
# this script). Manually placed or baked-in directories without .git are kept.
expected_names=""
for src in $(echo "$COL_LIST" | sed 's/,/ /g'); do
  repo="${src%%#*}"
  expected_names="$expected_names $(basename "$repo" .git)"
done

for d in "$COL_DIR"/*/; do
  [ -d "$d/.git" ] || continue
  dname=$(basename "$d")
  found=0
  for n in $expected_names; do
    [ "$n" = "$dname" ] && found=1 && break
  done
  if [ $found -eq 0 ]; then
    echo "Pruning removed collection: $dname"
    rm -rf "$d"
  fi
done
