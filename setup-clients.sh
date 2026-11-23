#!/bin/sh
# The two separately licensed official CLIs are installed in this checkout only.
set -eu
umask 077
cd -P -- "$(dirname -- "$0")"
dest="$PWD/bin/clients"
if [ -d "$dest" ]; then
  "$dest/node_modules/.bin/codex" --version
  "$dest/node_modules/.bin/claude" --version
  exit 0
fi
stage=$(mktemp -d "$PWD/bin/.clients-XXXXXX")
trap 'test ! -d "$stage" || /bin/rm -rf "$stage"' EXIT
cp runtime/package*.json "$stage/"
npm ci --prefix "$stage" --no-audit --no-fund
"$stage/node_modules/.bin/codex" --version | grep -qx 'codex-cli 0.171.0'
"$stage/node_modules/.bin/claude" --version | grep -q '^2\.1\.330 '
python3 -c 'import os,sys; os.rename(sys.argv[1],sys.argv[2])' "$stage" "$dest"
printf '%s\n' 'Official clients are ready in bin/clients. Global commands are unchanged.'
