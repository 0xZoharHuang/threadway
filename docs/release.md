# Release checklist

Use a matching macOS or Linux release host with the supported client releases, Python 3.9+ (Python 3.11+ for packaging), Rust 1.96, a native compiler and npm. For a new client version, inspect the upstream native schema, provider behavior, writer locks and picker before relaxing any version guard. Update exact npm dependency versions/lockfile, Rust dependency/lockfile, fixture assertions and qualification notes as one reviewable change.

Run `./build.sh`, the unit tests, and the full `tests/native_e2e.py` suite on official release binaries. Run the Google and Playwright variants separately with their result recorded. Check native picker, correct model-visible order, duplicate-free repeated switch, active writer refusal, divergent native turns, both fork directions and recovery. An unverified target stays explicitly staging-only.

On a clean commit, `python3 scripts/package.py /path/to/threadway-macos-arm64.tar.gz` packs tracked source, the compiled native codec and Rust dependency license notices. The hash is printed. Test the extracted binary and shell launcher. Official clients are acquired separately with the npm lockfile and retain their vendor licenses. Local archives are unsigned. Never package `bin/clients`, tool homes, the Threadway local index, staging/recovery data, private paths or operator transcripts.
