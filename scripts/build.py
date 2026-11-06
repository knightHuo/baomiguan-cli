"""Build a Windows single-file executable and its release checksum."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上构建 Windows 发行程序。")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--console", "--name", "Baomiguan",
            "--exclude-module", "PIL", "--exclude-module", "pytest",
            "--add-data", f"LICENSE{os.pathsep}.",
            "--add-data", f"THIRD_PARTY_NOTICES.md{os.pathsep}.",
            "--hidden-import", "keyring.backends.Windows",
            "--recursive-copy-metadata", "rich",
            "--recursive-copy-metadata", "requests",
            "--recursive-copy-metadata", "keyring",
            "--recursive-copy-metadata", "pycryptodome",
            "--recursive-copy-metadata", "qrcode",
            "main.py",
        ],
        cwd=ROOT,
        check=True,
    )
    executable = ROOT / "dist" / "Baomiguan.exe"
    checksum = hashlib.sha256(executable.read_bytes()).hexdigest()
    (ROOT / "dist" / "SHA256SUMS").write_text(
        f"{checksum}  {executable.name}\n", encoding="utf-8"
    )
    print(f"Built {executable.name} ({executable.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
