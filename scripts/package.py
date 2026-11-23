"""Create a portable arm64 archive with the owned binary, tracked source and dependency notices."""

import argparse
import gzip
import hashlib
import io
import os
import platform
from pathlib import Path
import subprocess
import tarfile
import tomllib

repo = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("output", type=Path)
    dest = cli.parse_args().output.resolve()
    if git("status", "--porcelain").strip():
        raise SystemExit("Package from a clean commit.")
    names = [os.fsdecode(q) for q in git("ls-files", "-z").split(b"\0") if q]
    binary = repo / "bin/threadway-codec"
    desc = subprocess.check_output(["file", str(binary)], text=True)
    host = platform.system()
    arch = platform.machine()
    if (host, arch) not in (
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
    ):
        raise SystemExit("Unsupported release host")
    if ("Mach-O" if host == "Darwin" else "ELF") not in desc:
        raise SystemExit("Build the native codec for this host first")
    names += ["bin/threadway-codec"]
    version = tomllib.loads((repo / "Cargo.toml").read_text())["package"]["version"]
    prefix = f"threadway-{version}-{'macos' if host == 'Darwin' else 'linux'}-{arch}"
    stamp = int(git("show", "-s", "--format=%ct", "HEAD"))
    notices = []
    registry = (
        Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry/src"
    )
    for dep in tomllib.loads((repo / "Cargo.lock").read_text())["package"]:
        if "source" not in dep:
            continue
        paths = list(registry.glob("*/" + dep["name"] + "-" + dep["version"]))
        if not paths:
            continue  # target-only dependency not built locally
        manifest = tomllib.loads((paths[0] / "Cargo.toml").read_text())["package"]
        notices.append(
            f"\n{dep['name']} {dep['version']} · {manifest.get('license', 'see license file')}\n"
        )
        for p in sorted(paths[0].iterdir()):
            if p.is_file() and p.name.lower().startswith(
                ("license", "licence", "copying", "copyright", "notice", "unlicense")
            ):
                notices.append(p.read_text(errors="replace"))
    if not notices:
        raise SystemExit("Missing crate registry notices")
    with (
        dest.open("xb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=stamp) as gz,
        tarfile.open(fileobj=gz, mode="w|") as tar,
    ):

        def add(name, data, mode):
            info = tarfile.TarInfo(prefix + "/" + name)
            info.size = len(data)
            info.mode = mode
            info.mtime = stamp
            tar.addfile(info, io.BytesIO(data))

        for name in names:
            p = repo / name
            if p.is_symlink() or not p.is_file():
                raise ValueError(name)
            add(name, p.read_bytes(), 0o755 if os.access(p, os.X_OK) else 0o644)
        add("THIRD-PARTY-NOTICES.txt", "\n".join(notices).encode(), 0o644)
        add("REVISION", git("rev-parse", "HEAD"), 0o644)
    print(hashlib.sha256(dest.read_bytes()).hexdigest(), dest.name)


if __name__ == "__main__":
    main()
