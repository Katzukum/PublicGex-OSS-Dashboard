"""Build the analytics executable in this project; never package user data or keys."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    backend = root / "backend"
    build = root / "build" / "sidecar"
    binaries = root / "src-tauri" / "binaries"
    local_rustc = root / ".tools" / "cargo" / "bin" / ("rustc.exe" if os.name == "nt" else "rustc")
    rustc = str(local_rustc) if local_rustc.is_file() else shutil.which("rustc")
    if not rustc:
        raise SystemExit("Rust is missing. Run scripts/native-setup.ps1 first.")
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(root / ".tools" / "pyinstaller")
    if local_rustc.is_file():
        env["CARGO_HOME"] = str(root / ".tools" / "cargo")
        env["RUSTUP_HOME"] = str(root / ".tools" / "rustup")
    target = subprocess.check_output([rustc, "--print", "host-tuple"], env=env, text=True).strip()
    subprocess.run([sys.executable, "-c", "import PyInstaller"], check=True)
    build.mkdir(parents=True, exist_ok=True)
    binaries.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "publicgex-backend", "--paths", str(backend),
        "--distpath", str(build / "dist"), "--workpath", str(build / "work"),
        "--specpath", str(build), "--collect-all", "public_api_sdk",
    ]
    # Domain modules are loaded through the RPC registry and collector subprocess.
    for source in sorted(backend.glob("*.py")):
        if source.stem != "service" and not source.stem.startswith("test_"):
            command.extend(["--hidden-import", source.stem])
    command.append(str(backend / "service.py"))
    subprocess.run(command, cwd=root, env=env, check=True)
    suffix = ".exe" if os.name == "nt" else ""
    output = binaries / f"publicgex-backend-{target}{suffix}"
    shutil.copy2(build / "dist" / f"publicgex-backend{suffix}", output)
    print(f"Built analytics sidecar: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
