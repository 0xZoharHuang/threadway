#!/bin/sh
set -eu
cd -P -- "$(dirname -- "$0")"
RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$PWD=threadway --remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=cargo" cargo build --locked --release
mkdir -p bin
install -m 755 "${CARGO_TARGET_DIR:-target}/release/threadway-codec" bin/threadway-codec
printf '%s\n' "Ready: $PWD/bin/threadway --help"
