"""Install a checksum-pinned vendor CLI into this checkout; use no account credentials."""

import hashlib
import os
import platform
from pathlib import Path
import subprocess
import tarfile
import tempfile

VERSION = "1.5.15"
DIGESTS = {
    ("Darwin", "arm64"): (
        "mac_arm64",
        "9e25108f3ea70cc6c846d88b9397456d13ae472a010016c3d625eb3fe99afc3a",
    ),
    ("Darwin", "x86_64"): (
        "mac_x64",
        "91fee043a384b76cee5d5f1568b946ab2f25bf38c938f0bd7c4b27fefa0c4e43",
    ),
    ("Linux", "x86_64"): (
        "linux_x64",
        "8d9913298602049ef29e9669f35252b6b79571979ce1bd0fb27753c7bac84114",
    ),
    ("Linux", "aarch64"): (
        "linux_arm64",
        "e5b5d7ca1dda2cfdc0b09c72931320c3ef5c481a14dbda137304107ce6e776b1",
    ),
}


def main():
    spec = DIGESTS.get((platform.system(), platform.machine()))
    if not spec:
        raise SystemExit("Unsupported native host. See the release matrix.")
    asset, check = spec
    dest = Path(__file__).resolve().parents[1] / "bin/google/antigravity"
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        archive = Path(tmp) / "archive"
        url = f"https://github.com/google-antigravity/antigravity-cli/releases/download/{VERSION}/agy_cli_{asset}.tar.gz"
        # curl honors the OS trust store and works on clean macOS installations.
        subprocess.run(
            [
                "curl",
                "--proto",
                "=https",
                "--retry",
                "3",
                "-fsSL",
                url,
                "-o",
                str(archive),
            ],
            check=True,
        )
        h = hashlib.sha256()
        with archive.open("rb") as f:
            for block in iter(lambda: f.read(1048576), b""):
                h.update(block)
        if h.hexdigest() != check:
            raise SystemExit("Official archive checksum mismatch")
        with tarfile.open(archive) as tf:
            binaries = [
                m
                for m in tf.getmembers()
                if m.isfile() and Path(m.name).name in ("agy", "antigravity")
            ]
            if len(binaries) != 1:
                raise SystemExit("Unexpected official archive layout")
            p = Path(tmp) / "antigravity"
            with tf.extractfile(binaries[0]) as source, p.open("wb") as out:
                import shutil

                shutil.copyfileobj(source, out)
        p.chmod(0o755)
        if (
            subprocess.check_output([p, "--version"], text=True, timeout=10).strip()
            != VERSION
        ):
            raise SystemExit("Unexpected official CLI version")
        os.replace(p, dest)
    print("Google Antigravity CLI " + VERSION + " is ready in bin/google.")


if __name__ == "__main__":
    main()
