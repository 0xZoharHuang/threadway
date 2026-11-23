#!/bin/sh
set -eu
cd -P -- "$(dirname -- "$0")"
exec python3 -B scripts/setup_google.py
