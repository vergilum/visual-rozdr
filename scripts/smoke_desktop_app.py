"""Run the actual packaged Electron main/renderer with an isolated profile."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile


def check(app: Path) -> None:
    app = app.resolve()
    if sys.platform == "darwin":
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        executable = app / "Contents" / "MacOS" / "Visual Pozdr"
    else:
        executable = app / "Visual Pozdr.exe"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="visual-pozdr-gui-") as temporary:
        env = {**os.environ, "VISUAL_POZDR_SMOKE_PROFILE": temporary,
               "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_USERNAME": "visual_pozdr_demo_bot",
               "PORT": str(port), "DATA_DIR": "data",
               "DATABASE_PATH": str(Path(temporary) / "data" / "app.sqlite3")}
        env.pop("ELECTRON_RUN_AS_NODE", None)
        result = subprocess.run([str(executable), "--smoke-test"], env=env,
                                capture_output=True, text=True, timeout=120)
        print(result.stdout)
        print(result.stderr)
        if result.returncode != 0 or "PASS: packaged Electron" not in result.stdout:
            log = Path(temporary) / "logs" / "backend.log"
            if log.exists():
                print(log.read_text(encoding="utf-8", errors="replace"))
            if sys.platform == "darwin":
                reports = sorted((Path.home() / "Library/Logs/DiagnosticReports").glob("Visual Pozdr*.ips"),
                                 key=lambda entry: entry.stat().st_mtime, reverse=True)
                for report in reports[:3]:
                    print(f"Crash report: {report.name}")
                    try:
                        text = report.read_text(encoding="utf-8")
                        data = json.loads(text.split("\n", 1)[1])
                        print(json.dumps({key: data.get(key) for key in
                                          ("procName", "exception", "termination", "asi", "faultingThread")}, indent=2))
                        for thread in data.get("threads", []):
                            if thread.get("triggered"):
                                print(json.dumps(thread, indent=2)[:16000])
                    except (ValueError, OSError) as error:
                        print(f"Could not decode crash report: {error}")
            raise RuntimeError(f"Electron smoke failed: {result.returncode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    check(parser.parse_args().app)
