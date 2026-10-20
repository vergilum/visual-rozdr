from __future__ import annotations

import subprocess
import sys
import platform
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "backend"
WORK = ROOT / "build" / "pyinstaller"
RUNTIME_FFMPEG = ROOT / "runtime" / "ffmpeg"
if sys.platform == "darwin":
    RUNTIME_FFMPEG /= "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64"


def main() -> int:
    suffix = ".exe" if sys.platform == "win32" else ""
    missing_tools = [name + suffix for name in ("ffmpeg", "ffprobe") if not (RUNTIME_FFMPEG / (name + suffix)).is_file()]
    if missing_tools:
        print(f"ERROR: bundled runtime tools are missing: {', '.join(missing_tools)} in {RUNTIME_FFMPEG}")
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "visual-pozdr-backend",
        "--distpath",
        str(ARTIFACTS),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        "--collect-all",
        "aiogram",
        "--collect-all",
        "pydantic",
        "--collect-all",
        "pydantic_settings",
        str(ROOT / "run_server.py"),
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
